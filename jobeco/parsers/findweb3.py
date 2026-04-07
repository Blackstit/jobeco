"""
Parser for findweb3.com — scrapes the Next.js job board via embedded __NEXT_DATA__ JSON.
Listing page provides job IDs; individual job pages provide full structured data.
"""
from __future__ import annotations

import asyncio
import json
import re
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
)
from jobeco.openrouter.client import (
    analyze_with_openrouter,
    embed_text,
    score_vacancy_with_openrouter,
    resolve_company_info,
    enrich_company_profile,
)

from sqlalchemy import select, func as sqla_func
from jobeco.processing.ats_enricher import fetch_ats_description

log = structlog.get_logger()

SOURCE_SLUG = "findweb3"
SOURCE_CHANNEL = "web:findweb3"
BASE_URL = "https://findweb3.com"

_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
}

_NEXT_DATA_RE = re.compile(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _extract_next_data(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ not found in page HTML")
    return json.loads(m.group(1))


async def fetch_job_ids(client: httpx.AsyncClient) -> list[str]:
    """Fetch the listing page and return all job record IDs."""
    resp = await client.get(f"{BASE_URL}/jobs", headers=_HEADERS)
    resp.raise_for_status()
    data = _extract_next_data(resp.text)
    jobs = data.get("props", {}).get("pageProps", {}).get("jobs", [])
    return [j["id"] for j in jobs if j.get("id")]


async def fetch_job_detail(client: httpx.AsyncClient, job_id: str) -> dict | None:
    """Fetch a single job page and extract structured fields from __NEXT_DATA__."""
    resp = await client.get(f"{BASE_URL}/job/{job_id}", headers=_HEADERS)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = _extract_next_data(resp.text)
    job = data.get("props", {}).get("pageProps", {}).get("job", {})
    fields = job.get("fields", {})
    if not fields:
        return None
    fields["_record_id"] = job.get("id") or job_id
    return fields


def _compose_raw_text(fields: dict, ats_description: str | None = None) -> str:
    """Compose a raw text block from the structured data for LLM analysis."""
    parts = []
    if fields.get("Job Title"):
        parts.append(f"Job title: {fields['Job Title']}")
    if fields.get("Company Name"):
        parts.append(f"Company: {fields['Company Name']}")
    if fields.get("Category"):
        parts.append(f"Category: {fields['Category']}")
    if fields.get("Company HQ"):
        parts.append(f"Location: {fields['Company HQ']}")
    if fields.get("Fully Remote?") == "Yes":
        parts.append("Remote: Yes (fully remote)")
    if fields.get("Pay"):
        parts.append(f"Salary: {fields['Pay']}")
    if fields.get("Job Type"):
        jt = fields["Job Type"]
        if isinstance(jt, list):
            jt = ", ".join(jt)
        parts.append(f"Employment type: {jt}")
    if fields.get("Tags"):
        parts.append(f"Tags: {', '.join(fields['Tags'])}")
    if ats_description:
        parts.append(f"\nFull Job Description (from company ATS):\n{ats_description}")
    elif fields.get("Job Description"):
        parts.append(f"\nDescription:\n{fields['Job Description']}")
    if fields.get("How to Apply"):
        parts.append(f"\nApply: {fields['How to Apply']}")
    return "\n".join(parts)


def _external_id(fields: dict) -> str:
    rec_id = fields.get("_record_id") or ""
    job_num = fields.get("Job ID", "")
    return f"findweb3:{rec_id or job_num}"


async def _already_exists(external_id: str) -> bool:
    async with SessionLocal() as s:
        row = await s.execute(
            select(Vacancy.id).where(Vacancy.external_id == external_id).limit(1)
        )
        return row.scalar() is not None


async def process_web_vacancy(fields: dict) -> int | None:
    """
    Run a single findweb3.com job through the full pipeline.
    Returns vacancy_id or None if skipped.
    """
    ext_id = _external_id(fields)
    if await _already_exists(ext_id):
        return None

    raw_text = _compose_raw_text(fields)
    if not raw_text.strip():
        return None

    # Enrich with full ATS description (Ashby, Greenhouse, Lever, Workday, etc.)
    ats_text = None
    apply_url = fields.get("How to Apply")
    if apply_url:
        try:
            ats_text = await fetch_ats_description(apply_url)
            if ats_text and len(ats_text) > len(fields.get("Job Description") or ""):
                raw_text = _compose_raw_text(fields, ats_description=ats_text)
                log.info("ats_enriched", source=SOURCE_SLUG, title=fields.get("Job Title"),
                         ats_chars=len(ats_text))
        except Exception as exc:
            log.debug("ats_enrich_failed", url=apply_url, error=str(exc))

    embedding = await embed_text(raw_text)
    if embedding and await is_duplicate(embedding):
        log.info("web_dedup_skip", source=SOURCE_SLUG, title=fields.get("Job Title"))
        return None

    analysis = await analyze_with_openrouter(raw_text)

    company_info = await resolve_company_info(
        company_name=analysis.get("company_name") or fields.get("Company Name"),
        raw_text=raw_text,
        llm_website=analysis.get("company_website"),
        llm_linkedin=analysis.get("company_linkedin"),
    )

    try:
        scoring = await score_vacancy_with_openrouter(raw_text, analysis)
    except Exception:
        scoring = {"total_score": 5.0, "scoring_results": [], "overall_summary": "", "red_flags": []}

    total_score = scoring.get("total_score")
    try:
        ai_score = int(round(float(total_score)))
    except Exception:
        ai_score = int(analysis.get("ai_score_value") or 5)
    ai_score = max(0, min(10, ai_score))

    company_profile = {}
    try:
        company_profile = await enrich_company_profile(
            company_name=analysis.get("company_name") or fields.get("Company Name"),
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

    display_company_url = pick_corporate_website(
        company_info.get("company_url"), company_profile.get("website")
    )

    company_id = await upsert_company(
        company_name=analysis.get("company_name") or fields.get("Company Name"),
        company_profile=company_profile,
        company_url=company_info.get("company_url"),
        company_linkedin=company_info.get("company_linkedin"),
    )

    vacancy_domains = [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()]

    contacts = list(analysis.get("contacts") or [])
    if fields.get("How to Apply"):
        contacts.append(fields["How to Apply"])
    contacts = _enrich_contacts_with_forms(contacts, raw_text)

    page_url = f"{BASE_URL}/job/{fields.get('_record_id', '')}"

    payload = {
        "source_url": page_url,
        "external_id": ext_id,
        "source_channel": SOURCE_CHANNEL,
        "company_name": analysis.get("company_name") or fields.get("Company Name"),
        "company_url": display_company_url,
        "title": analysis.get("title") or fields.get("Job Title"),
        "location_type": analysis.get("location_type"),
        "salary_min_usd": analysis.get("salary_min_usd"),
        "salary_max_usd": analysis.get("salary_max_usd"),
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
            "company_linkedin_verified": company_info.get("company_linkedin_verified", False),
            "employment_type": analysis.get("employment_type"),
            "language_requirements": analysis.get("language_requirements"),
            "company_profile": company_profile if company_profile else None,
            "web_source": SOURCE_SLUG,
            "findweb3_tags": fields.get("Tags", []),
            "findweb3_category": fields.get("Category"),
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

    try:
        vacancy_id = await save_vacancy(payload, embedding)
    except Exception as exc:
        if "UniqueViolation" in str(type(exc).__mro__) or "unique" in str(exc).lower():
            log.info("web_dup_ext_id", source=SOURCE_SLUG, ext_id=ext_id)
            return None
        raise
    if vacancy_id:
        await persist_parser_log(
            level="INFO",
            event="vacancy_added",
            message_en=f"Web vacancy added from {SOURCE_SLUG}: {fields.get('Job Title')} @ {fields.get('Company Name')}. ID {vacancy_id}",
            vacancy_id=vacancy_id,
        )
    return vacancy_id


async def sync_source(max_pages: int = 1, limit: int = 20) -> dict:
    """
    Sync jobs from findweb3.com.
    max_pages is unused (single-page listing), kept for interface consistency.
    Returns a summary dict with counts and errors.
    """
    result = {"added": 0, "skipped": 0, "errors": [], "total_fetched": 0}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            job_ids = await fetch_job_ids(client)
        except Exception as e:
            result["errors"].append(f"Listing fetch failed: {e}")
            return result

        result["total_fetched"] = len(job_ids)
        processed = 0

        for jid in job_ids:
            if processed >= limit:
                break
            try:
                fields = await fetch_job_detail(client, jid)
                if not fields:
                    result["skipped"] += 1
                    processed += 1
                    continue

                vid = await process_web_vacancy(fields)
                if vid:
                    result["added"] += 1
                else:
                    result["skipped"] += 1
                processed += 1
            except Exception as e:
                err = f"Job {jid}: {e}"
                log.error("web_vacancy_error", source=SOURCE_SLUG, error=str(e), job_id=jid)
                result["errors"].append(err)
                processed += 1

            await asyncio.sleep(1.0)

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
    """Create the web_sources row for findweb3 if it doesn't exist."""
    async with SessionLocal() as s:
        existing = (
            await s.execute(
                select(WebSource).where(WebSource.slug == SOURCE_SLUG)
            )
        ).scalar_one_or_none()
        if not existing:
            ws = WebSource(
                slug=SOURCE_SLUG,
                name="FindWeb3",
                url="https://findweb3.com/jobs",
                parser_type="findweb3",
                enabled=True,
                sync_interval_minutes=360,
                max_pages=1,
            )
            s.add(ws)
            await s.commit()
