"""
Parser for remocate.app — Webflow-based job board with SSR HTML.
Fetches the listing page for job slugs and salary data,
then loads individual pages for full descriptions and apply URLs.
"""
from __future__ import annotations

import asyncio
import re
import json
from datetime import datetime, timezone

import httpx
import structlog
from bs4 import BeautifulSoup

from jobeco.db.session import SessionLocal
from jobeco.db.models import Vacancy, WebSource
from jobeco.processing.company_branding import pick_corporate_website
from jobeco.processing.pipeline import (
    save_vacancy,
    persist_parser_log,
    is_duplicate,
    upsert_company,
    _enrich_contacts_with_forms,
    _boost_company_score,
)
from jobeco.openrouter.client import (
    analyze_with_openrouter,
    embed_text,
    score_vacancy_with_openrouter,
    resolve_company_info,
    enrich_company_profile,
)

from sqlalchemy import select, func as sqla_func

log = structlog.get_logger()

SOURCE_SLUG = "remocate"
SOURCE_CHANNEL = "web:remocate"
BASE_URL = "https://www.remocate.app"

_HEADERS = {
    "accept": "text/html,application/xhtml+xml",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_JOB_HREF_RE = re.compile(r"^/jobs/[\w-]+$")
_SALARY_RE = re.compile(r"\$([\d,]+)\s*[–—-]\s*\$([\d,]+)")


def _strip_html(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()


def _parse_int(val: str) -> int | None:
    try:
        return int(val.replace(",", ""))
    except (ValueError, TypeError):
        return None


async def _get_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True)


async def fetch_listing() -> list[dict]:
    """
    Fetch main listing page; return list of dicts with slug, salary_min, salary_max.
    """
    async with await _get_client() as client:
        resp = await client.get(BASE_URL + "/", headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")

    cards: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=_JOB_HREF_RE):
        href = a["href"]
        slug = href.split("/jobs/", 1)[-1]
        if not slug or slug in seen:
            continue
        seen.add(slug)

        card: dict = {"slug": slug, "href": href}

        item = a.find_parent("div", class_="w-dyn-item")
        if item:
            text = item.get_text(" ", strip=True)
            m = _SALARY_RE.search(text)
            if m:
                card["salary_min"] = _parse_int(m.group(1))
                card["salary_max"] = _parse_int(m.group(2))

        cards.append(card)

    return cards


async def fetch_job_page(slug: str) -> dict | None:
    """
    Fetch an individual job page; return parsed fields.
    """
    url = f"{BASE_URL}/jobs/{slug}"
    async with await _get_client() as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    data: dict = {"slug": slug, "page_url": url}

    h1 = soup.find("h1")
    data["title"] = h1.get_text(strip=True) if h1 else None

    og = soup.find("meta", attrs={"property": "og:title"})
    if og:
        m = re.search(r" at (.+?)(?:\s*-\s*Remocate)?$", og.get("content", ""))
        if m:
            data["company"] = m.group(1).strip()

    comp_el = soup.find("div", class_="job-top-company")
    if comp_el:
        data["company"] = comp_el.get_text(strip=True)

    tags = soup.find_all("div", class_="job-tag")
    tag_texts = [t.get_text(strip=True) for t in tags]
    visible_tags = []
    for t in tags:
        classes = " ".join(t.get("class", []))
        if "w-condition-invisible" not in classes:
            visible_tags.append(t.get_text(strip=True))

    for txt in visible_tags:
        if txt in ("Full-time", "Part-time", "Contract"):
            data["employment_type"] = txt
        elif txt in ("Junior", "Middle", "Senior", "Lead", "Head", "Chief", "Principal", "Staff"):
            data["seniority"] = txt
        elif txt.startswith("💻") or txt.startswith("🚀") or txt.startswith("📊") or \
             txt.startswith("🕵") or txt.startswith("💈") or txt.startswith("🧾") or \
             txt.startswith("🪄") or txt.startswith("📨") or txt.startswith("📞") or \
             txt.startswith("🤔") or txt.startswith("💵") or txt.startswith("⚖") or \
             txt.startswith("💣") or txt.startswith("📚") or txt.startswith("🧑"):
            data["category"] = txt
        elif txt.startswith("🏠"):
            data.setdefault("work_type", []).append("Remote")
        elif txt.startswith("✈"):
            data.setdefault("work_type", []).append("Relocation")
        elif re.match(r"^[🇦-🇿]{2}\s", txt) or txt.startswith("🌎"):
            data["location"] = txt

    date_el = soup.find("div", class_="job-top-right")
    if date_el:
        data["published_date"] = date_el.get_text(strip=True)

    apply_url = None
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True)
        href = a["href"]
        if "apply" in txt.lower() and href.startswith("http") and "remocate" not in href:
            apply_url = href
            break
    data["apply_url"] = apply_url

    contacts: list[str] = []
    if apply_url:
        contacts.append(apply_url)

    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        email = a["href"].replace("mailto:", "").split("?")[0].strip()
        if email and "remocate" not in email.lower() and email not in contacts:
            contacts.append(email)

    all_text = soup.get_text(" ", strip=True)
    for em in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", all_text[:5000]):
        if "remocate" not in em.lower() and em not in contacts:
            if not re.match(r".*\.(js|css|png|svg|jpg|webp)$", em):
                contacts.append(em)

    for tg in re.findall(r"(?:📩\s*Apply:\s*|Apply:\s*|Contact:\s*)(@[\w]+)", all_text[:5000]):
        handle = f"https://t.me/{tg.lstrip('@')}"
        if handle not in contacts:
            contacts.append(handle)

    data["contacts"] = contacts

    rt_blocks = soup.find_all("div", class_=re.compile(r"w-richtext"))
    desc_parts: list[str] = []
    for rt in rt_blocks:
        parent = rt.find_parent("div", class_="w-dyn-item")
        if parent and parent.find("a", href=re.compile(r"/jobs/")):
            continue
        txt = rt.get_text("\n", strip=True)
        if txt and len(txt) > 30:
            desc_parts.append(txt)
    data["description"] = "\n\n".join(desc_parts[:3]) if desc_parts else None

    logo = soup.find("img", class_="job-top-logo")
    if logo and logo.get("src"):
        data["logo_url"] = logo["src"]

    return data


def _compose_raw_text(listing: dict, detail: dict) -> str:
    parts: list[str] = []

    title = detail.get("title")
    if title:
        parts.append(f"Job title: {title}")

    company = detail.get("company")
    if company:
        parts.append(f"Company: {company}")

    loc = detail.get("location")
    if loc:
        parts.append(f"Location: {loc}")

    wt = detail.get("work_type")
    if wt:
        parts.append(f"Work type: {', '.join(wt)}")

    emp = detail.get("employment_type")
    if emp:
        parts.append(f"Employment: {emp}")

    sen = detail.get("seniority")
    if sen:
        parts.append(f"Seniority: {sen}")

    sal_min = listing.get("salary_min") or detail.get("salary_min")
    sal_max = listing.get("salary_max") or detail.get("salary_max")
    if sal_min and sal_max:
        parts.append(f"Salary: ${sal_min:,} – ${sal_max:,} USD")
    elif sal_min:
        parts.append(f"Salary from: ${sal_min:,} USD")

    desc = detail.get("description")
    if desc:
        parts.append(f"\nDescription:\n{desc[:4000]}")

    page_url = detail.get("page_url")
    if page_url:
        parts.append(f"\nSource: {page_url}")

    apply_url = detail.get("apply_url")
    if apply_url:
        parts.append(f"Apply: {apply_url}")

    return "\n".join(parts)


def _external_id(slug: str) -> str:
    return f"remo:{slug}"


async def _already_exists(external_id: str) -> bool:
    async with SessionLocal() as s:
        row = await s.execute(
            select(Vacancy.id).where(Vacancy.external_id == external_id).limit(1)
        )
        return row.scalar() is not None


async def process_remocate_vacancy(
    listing: dict,
    detail: dict,
) -> int | None:
    slug = detail["slug"]
    ext_id = _external_id(slug)
    if await _already_exists(ext_id):
        return None

    raw_text = _compose_raw_text(listing, detail)
    if not raw_text.strip():
        return None

    embedding = await embed_text(raw_text)
    if embedding and await is_duplicate(embedding):
        log.info("web_dedup_skip", source=SOURCE_SLUG, title=detail.get("title"))
        return None

    analysis = await analyze_with_openrouter(raw_text)

    company_name = analysis.get("company_name") or detail.get("company")

    company_info = await resolve_company_info(
        company_name=company_name,
        raw_text=raw_text,
        llm_website=analysis.get("company_website"),
        llm_linkedin=analysis.get("company_linkedin"),
    )

    try:
        scoring = await score_vacancy_with_openrouter(raw_text, analysis)
    except Exception:
        scoring = {
            "total_score": 5.0,
            "scoring_results": [],
            "overall_summary": "",
            "red_flags": [],
        }

    total_score = scoring.get("total_score")
    try:
        ai_score = int(round(float(total_score)))
    except Exception:
        ai_score = int(analysis.get("ai_score_value") or 5)
    ai_score = max(0, min(10, ai_score))

    company_profile = {}
    try:
        company_profile = await enrich_company_profile(
            company_name=company_name,
            company_url=company_info.get("company_url"),
        )
    except Exception:
        pass

    # Job page often has the real employer logo (CDN); prefer over favicon-from-ATS.
    page_logo = detail.get("logo_url")
    if page_logo:
        company_profile = {**company_profile, "logo_url": page_logo}

    scoring = _boost_company_score(scoring, company_profile, company_info)
    total_score = scoring.get("total_score")
    try:
        ai_score = int(round(float(total_score)))
    except Exception:
        pass
    ai_score = max(0, min(10, ai_score))

    display_company_url = pick_corporate_website(company_info.get("company_url"), company_profile.get("website"))

    company_id = await upsert_company(
        company_name=company_name,
        company_profile=company_profile,
        company_url=company_info.get("company_url"),
        company_linkedin=company_info.get("company_linkedin"),
    )

    vacancy_domains = [
        str(x).lower()
        for x in (analysis.get("domains") or [])
        if str(x).strip()
    ]

    contacts = list(detail.get("contacts") or [])
    for c in (analysis.get("contacts") or []):
        if c not in contacts:
            contacts.append(c)
    page_url = detail.get("page_url")
    if not contacts and page_url:
        contacts.append(page_url)
    contacts = _enrich_contacts_with_forms(contacts, raw_text)

    sal_min = analysis.get("salary_min_usd") or listing.get("salary_min")
    sal_max = analysis.get("salary_max_usd") or listing.get("salary_max")

    payload = {
        "source_url": page_url,
        "external_id": ext_id,
        "source_channel": SOURCE_CHANNEL,
        "company_name": company_name,
        "company_url": display_company_url,
        "title": analysis.get("title") or detail.get("title"),
        "location_type": analysis.get("location_type"),
        "salary_min_usd": sal_min,
        "salary_max_usd": sal_max,
        "stack": analysis.get("stack", []),
        "category": analysis.get("category"),
        "ai_score_value": ai_score,
        "summary_ru": analysis.get("summary_ru"),
        "summary_en": analysis.get("summary_en"),
        "raw_text": raw_text,
        "metadata": {
            **(analysis.get("metadata", {}) or {}),
            "scoring": scoring,
            "company_linkedin": company_info.get("company_linkedin"),
            "company_url_verified": company_info.get("company_url_verified", False),
            "company_linkedin_verified": company_info.get(
                "company_linkedin_verified", False
            ),
            "employment_type": analysis.get("employment_type")
            or detail.get("employment_type"),
            "language_requirements": analysis.get("language_requirements"),
            "company_profile": company_profile if company_profile else None,
            "web_source": SOURCE_SLUG,
        },
        "company_id": company_id,
        "domains": vacancy_domains,
        "risk_label": analysis.get("risk_label"),
        "recruiter": analysis.get("recruiter"),
        "contacts": contacts,
        "description": analysis.get("description"),
        "responsibilities": analysis.get("responsibilities"),
        "requirements": analysis.get("requirements"),
        "conditions": analysis.get("conditions"),
        "role": analysis.get("role"),
        "seniority": (analysis.get("seniority") or detail.get("seniority") or "").lower().strip() or None,
        "english_level": (analysis.get("english_level") or "").strip().upper() or None,
        "standardized_title": analysis.get("standardized_title"),
        "language": analysis.get("language"),
    }

    vacancy_id = await save_vacancy(payload, embedding)
    if vacancy_id:
        await persist_parser_log(
            level="INFO",
            event="vacancy_added",
            message_en=f"Web vacancy added from {SOURCE_SLUG}. ID {vacancy_id}",
            vacancy_id=vacancy_id,
        )
    return vacancy_id


async def sync_source(max_pages: int = 1, limit: int = 15) -> dict:
    """Sync jobs from remocate.app. Returns a summary dict."""
    result = {"added": 0, "skipped": 0, "errors": [], "total_fetched": 0}
    processed = 0

    try:
        cards = await fetch_listing()
    except Exception as e:
        result["errors"].append(f"Listing: {e}")
        return result

    if not cards:
        return result

    result["total_fetched"] = len(cards)
    log.info("remo_listing", total=len(cards))

    for card in cards:
        if processed >= limit:
            break

        slug = card["slug"]
        try:
            detail = await fetch_job_page(slug)
        except Exception as e:
            result["errors"].append(f"Fetch {slug}: {e}")
            processed += 1
            continue

        if not detail or not detail.get("title"):
            result["skipped"] += 1
            processed += 1
            continue

        try:
            vid = await process_remocate_vacancy(
                listing=card,
                detail=detail,
            )
            if vid:
                result["added"] += 1
            else:
                result["skipped"] += 1
            processed += 1
        except Exception as e:
            title = detail.get("title", "?")
            err = f"Job '{title}' ({slug}): {e}"
            log.error("web_vacancy_error", source=SOURCE_SLUG, error=str(e))
            result["errors"].append(err)
            processed += 1

        await asyncio.sleep(0.5)

    try:
        async with SessionLocal() as s:
            ws = (
                await s.execute(
                    select(WebSource).where(WebSource.slug == SOURCE_SLUG)
                )
            ).scalar_one_or_none()
            if ws:
                count = (
                    await s.execute(
                        select(sqla_func.count(Vacancy.id)).where(
                            Vacancy.source_channel == SOURCE_CHANNEL
                        )
                    )
                ).scalar() or 0
                ws.last_synced_at = datetime.now(timezone.utc)
                ws.vacancies_count = count
                await s.commit()
    except Exception:
        pass

    await persist_parser_log(
        level="INFO",
        event="web_sync_complete",
        message_en=(
            f"Web sync {SOURCE_SLUG}: +{result['added']} added, "
            f"{result['skipped']} skipped, {len(result['errors'])} errors"
        ),
    )

    return result


async def ensure_source_record():
    """Create the web_sources row for remocate if it doesn't exist."""
    async with SessionLocal() as s:
        existing = (
            await s.execute(
                select(WebSource).where(WebSource.slug == SOURCE_SLUG)
            )
        ).scalar_one_or_none()
        if not existing:
            ws = WebSource(
                slug=SOURCE_SLUG,
                name="Remocate",
                url="https://www.remocate.app",
                parser_type="remocate",
                enabled=True,
                sync_interval_minutes=360,
                max_pages=1,
            )
            s.add(ws)
            await s.commit()
