"""
Parser for cryptojobs.com — fetches the /jobs listing page,
follows individual job links, extracts JSON-LD JobPosting data
and external apply URLs, then processes through the standard pipeline.
"""
from __future__ import annotations

import asyncio
import re
import json
from datetime import datetime, timezone

import httpx
import structlog

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
    try_enrich_from_ats,
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

SOURCE_SLUG = "cryptojobs"
SOURCE_CHANNEL = "web:cryptojobs"
BASE_URL = "https://www.cryptojobs.com"

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

_JOB_HREF_RE = re.compile(
    r'href="((?:https://www\.cryptojobs\.com)?/job/[^"]+)"'
)
_LD_PAT = re.compile(
    r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>',
    re.S,
)
_SOCIAL_DOMAINS = (
    "twitter.com", "facebook.com", "linkedin.com", "instagram.com",
    "youtube.com", "t.me", "sharer.", "intent/tweet",
)

_ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "breezy.hr",
    "workable.com", "bamboohr.com", "personio.com", "smartrecruiters.com",
    "recruitee.com", "workday.com", "myworkday", "icims.com",
    "taleo.", "jobvite.com", "ultipro.com",
)


def _strip_html(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()


async def _get_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True)


async def fetch_listing(page: int = 1) -> list[str]:
    """Fetch /jobs listing page and return unique job hrefs (absolute URLs)."""
    url = f"{BASE_URL}/jobs" if page <= 1 else f"{BASE_URL}/jobs?page={page}"
    async with await _get_client() as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

    seen = set()
    result = []
    for m in _JOB_HREF_RE.finditer(html):
        href = m.group(1)
        if href.startswith("/"):
            href = BASE_URL + href
        if "/job/" not in href or href in seen:
            continue
        if "/jobs/create" in href or "/jobs/feed" in href:
            continue
        seen.add(href)
        result.append(href)

    return result


async def fetch_job_page(url: str) -> tuple[dict | None, str | None]:
    """
    Fetch an individual job page and return (json_ld_dict, apply_url).
    apply_url is the best external link for applying.
    """
    async with await _get_client() as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

    ld = None
    for m in _LD_PAT.finditer(html):
        raw = re.sub(r"[\x00-\x1f]", " ", m.group(1).strip())
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("@type") == "JobPosting":
            ld = d
            break

    apply_url = None
    ext_links = re.findall(
        r'href="(https?://(?!www\.cryptojobs\.com|cdn\.|fonts\.|cdnjs\.)[^"]+)"',
        html,
    )
    for link in ext_links:
        if any(s in link for s in _SOCIAL_DOMAINS):
            continue
        if any(s in link for s in _ATS_DOMAINS):
            apply_url = link
            break
    if not apply_url:
        for link in ext_links:
            if any(s in link for s in _SOCIAL_DOMAINS):
                continue
            if re.search(r"/apply|/career|/jobs?/|/recruit|/hiring|/position", link, re.I):
                apply_url = link
                break

    return ld, apply_url


def _compose_raw_text(
    ld: dict,
    apply_url: str | None,
    page_url: str | None,
) -> str:
    parts = []

    title = ld.get("title", "")
    if title:
        parts.append(f"Job title: {title}")

    org = ld.get("hiringOrganization", {})
    if org.get("name"):
        parts.append(f"Company: {org['name']}")

    loc = ld.get("jobLocation")
    if loc:
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address", {})
        loc_parts = [addr.get("addressLocality"), addr.get("addressCountry")]
        loc_str = ", ".join(p for p in loc_parts if p and p != "Anywhere")
        if loc_str:
            parts.append(f"Location: {loc_str}")

    if ld.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Location type: Remote")

    emp = ld.get("employmentType")
    if emp:
        parts.append(f"Employment type: {emp}")

    sal = ld.get("baseSalary", {})
    if sal:
        val = sal.get("value", {})
        currency = sal.get("currency", "USD")
        unit = val.get("unitText", "YEAR")
        lo, hi = val.get("minValue"), val.get("maxValue")
        if lo and hi:
            parts.append(f"Salary: {lo}-{hi} {currency} / {unit}")

    category = ld.get("occupationalCategory")
    if category:
        parts.append(f"Category: {category}")

    desc = ld.get("description", "")
    if desc:
        plain = _strip_html(desc)
        parts.append(f"\nDescription:\n{plain}")

    if page_url:
        parts.append(f"\nSource: {page_url}")
    if apply_url:
        parts.append(f"Apply: {apply_url}")

    return "\n".join(parts)


def _slug_from_url(url: str) -> str:
    path = url.rstrip("/").rsplit("/", 1)[-1]
    return path


def _external_id(slug: str) -> str:
    return f"cj:{slug}"


async def _already_exists(external_id: str) -> bool:
    async with SessionLocal() as s:
        row = await s.execute(
            select(Vacancy.id).where(Vacancy.external_id == external_id).limit(1)
        )
        return row.scalar() is not None


async def process_cryptojobs_vacancy(
    job_url: str,
    ld: dict,
    apply_url: str | None,
) -> int | None:
    slug = _slug_from_url(job_url)
    ext_id = _external_id(slug)
    if await _already_exists(ext_id):
        return None

    raw_text = _compose_raw_text(ld, apply_url, job_url)
    if not raw_text.strip():
        return None

    embedding = await embed_text(raw_text)
    if embedding and await is_duplicate(embedding):
        log.info("web_dedup_skip", source=SOURCE_SLUG, title=ld.get("title"))
        return None

    raw_text = await try_enrich_from_ats(raw_text, apply_url)
    analysis = await analyze_with_openrouter(raw_text)

    org = ld.get("hiringOrganization", {})
    company_name = analysis.get("company_name") or org.get("name")

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

    contacts = list(analysis.get("contacts") or [])
    if apply_url:
        contacts.append(apply_url)
    elif job_url:
        contacts.append(job_url)
    contacts = _enrich_contacts_with_forms(contacts, raw_text)

    sal = ld.get("baseSalary", {})
    sal_val = sal.get("value", {}) if sal else {}
    sal_min = analysis.get("salary_min_usd")
    sal_max = analysis.get("salary_max_usd")
    if not sal_min and sal_val.get("minValue"):
        try:
            sal_min = int(float(sal_val["minValue"]))
        except (ValueError, TypeError):
            pass
    if not sal_max and sal_val.get("maxValue"):
        try:
            sal_max = int(float(sal_val["maxValue"]))
        except (ValueError, TypeError):
            pass

    payload = {
        "source_url": job_url,
        "external_id": ext_id,
        "source_channel": SOURCE_CHANNEL,
        "company_name": company_name,
        "company_url": display_company_url,
        "title": analysis.get("title") or ld.get("title"),
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
            or ld.get("employmentType"),
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
        "seniority": (analysis.get("seniority") or "").lower().strip() or None,
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


async def sync_source(max_pages: int = 1, limit: int = 10) -> dict:
    """Sync jobs from cryptojobs.com. Returns a summary dict."""
    result = {"added": 0, "skipped": 0, "errors": [], "total_fetched": 0}
    processed = 0

    for page in range(1, max_pages + 1):
        try:
            job_urls = await fetch_listing(page)
        except Exception as e:
            result["errors"].append(f"Page {page}: {e}")
            break

        if not job_urls:
            break

        result["total_fetched"] += len(job_urls)
        log.info(
            "cj_listing",
            page=page,
            total=len(job_urls),
        )

        for job_url in job_urls:
            if processed >= limit:
                break

            try:
                ld, apply_url = await fetch_job_page(job_url)
            except Exception as e:
                result["errors"].append(f"Fetch {job_url}: {e}")
                processed += 1
                continue

            if not ld:
                result["skipped"] += 1
                processed += 1
                continue

            try:
                vid = await process_cryptojobs_vacancy(
                    job_url=job_url,
                    ld=ld,
                    apply_url=apply_url,
                )
                if vid:
                    result["added"] += 1
                else:
                    result["skipped"] += 1
                processed += 1
            except Exception as e:
                title = ld.get("title", "?")
                err = f"Job '{title}': {e}"
                log.error("web_vacancy_error", source=SOURCE_SLUG, error=str(e))
                result["errors"].append(err)
                processed += 1

            await asyncio.sleep(0.5)

        if processed >= limit:
            break
        await asyncio.sleep(1)

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
    """Create the web_sources row for cryptojobs if it doesn't exist."""
    async with SessionLocal() as s:
        existing = (
            await s.execute(
                select(WebSource).where(WebSource.slug == SOURCE_SLUG)
            )
        ).scalar_one_or_none()
        if not existing:
            ws = WebSource(
                slug=SOURCE_SLUG,
                name="CryptoJobs.com",
                url="https://www.cryptojobs.com",
                parser_type="cryptojobs",
                enabled=True,
                sync_interval_minutes=360,
                max_pages=1,
            )
            s.add(ws)
            await s.commit()
