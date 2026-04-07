"""
Parser for degencryptojobs.com — fetches jobs from their public API
and processes them through the standard vacancy pipeline.
"""
from __future__ import annotations

import asyncio
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

SOURCE_SLUG = "degencryptojobs"
SOURCE_CHANNEL = "web:degencryptojobs"
API_BASE = "https://degencryptojobs.com/api/jobs"

_HEADERS = {
  "accept": "*/*",
  "accept-language": "en-US,en;q=0.9",
  "referer": "https://degencryptojobs.com/",
  "user-agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
  ),
}


async def fetch_page(page: int = 1) -> list[dict]:
  """Fetch a single page of jobs from the API."""
  async with httpx.AsyncClient(timeout=30) as client:
    resp = await client.get(f"{API_BASE}?page={page}", headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data.get("jobs", [])


def _compose_raw_text(job: dict) -> str:
  """Compose a raw text block from the structured API data for LLM analysis."""
  parts = []
  if job.get("title"):
    parts.append(f"Job title: {job['title']}")
  if job.get("company"):
    parts.append(f"Company: {job['company']}")
  if job.get("location"):
    parts.append(f"Location: {', '.join(job['location'])}")
  if job.get("salary"):
    parts.append(f"Salary: {job['salary']}")
  if job.get("tags") or job.get("topTags"):
    tags = list(set((job.get("tags") or []) + (job.get("topTags") or [])))
    parts.append(f"Tags: {', '.join(tags)}")
  if job.get("description"):
    parts.append(f"\nDescription:\n{job['description']}")
  if job.get("qualifications"):
    parts.append(f"\nQualifications:\n{job['qualifications']}")
  if job.get("link"):
    parts.append(f"\nApply: {job['link']}")
  return "\n".join(parts)


def _external_id(job: dict) -> str:
  """Generate a stable external_id from the job's unique link."""
  return f"degen:{job.get('link', '')}"


async def _already_exists(external_id: str) -> bool:
  async with SessionLocal() as s:
    row = await s.execute(
      select(Vacancy.id).where(Vacancy.external_id == external_id).limit(1)
    )
    return row.scalar() is not None


async def process_web_vacancy(job: dict) -> int | None:
  """
  Run a single web-sourced job through the full pipeline:
  prevalidation is skipped (it's already a job listing),
  but LLM analysis, scoring, company enrichment, and save all apply.
  Returns vacancy_id or None if skipped.
  """
  ext_id = _external_id(job)
  if await _already_exists(ext_id):
    return None

  raw_text = _compose_raw_text(job)
  if not raw_text.strip():
    return None

  embedding = await embed_text(raw_text)
  if embedding and await is_duplicate(embedding):
    log.info("web_dedup_skip", source=SOURCE_SLUG, title=job.get("title"))
    return None

  raw_text = await try_enrich_from_ats(raw_text, job.get("link"))
  analysis = await analyze_with_openrouter(raw_text)

  company_info = await resolve_company_info(
    company_name=analysis.get("company_name") or job.get("company"),
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
      company_name=analysis.get("company_name") or job.get("company"),
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
    company_name=analysis.get("company_name") or job.get("company"),
    company_profile=company_profile,
    company_url=company_info.get("company_url"),
    company_linkedin=company_info.get("company_linkedin"),
  )

  vacancy_domains = [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()]

  contacts = list(analysis.get("contacts") or [])
  if job.get("link"):
    contacts.append(job["link"])
  contacts = _enrich_contacts_with_forms(contacts, raw_text)

  payload = {
    "source_url": job.get("link"),
    "external_id": ext_id,
    "source_channel": SOURCE_CHANNEL,
    "company_name": analysis.get("company_name") or job.get("company"),
    "company_url": display_company_url,
    "title": analysis.get("title") or job.get("title"),
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


async def sync_source(max_pages: int = 1, limit: int = 20) -> dict:
  """
  Sync jobs from degencryptojobs.com.
  Returns a summary dict with counts and errors.
  """
  result = {"added": 0, "skipped": 0, "errors": [], "total_fetched": 0}
  processed = 0

  for page in range(1, max_pages + 1):
    try:
      jobs = await fetch_page(page)
    except Exception as e:
      result["errors"].append(f"Page {page}: {e}")
      break

    if not jobs:
      break

    result["total_fetched"] += len(jobs)

    for job in jobs:
      if processed >= limit:
        break
      try:
        vid = await process_web_vacancy(job)
        if vid:
          result["added"] += 1
        else:
          result["skipped"] += 1
        processed += 1
      except Exception as e:
        err = f"Job '{job.get('title', '?')}': {e}"
        log.error("web_vacancy_error", source=SOURCE_SLUG, error=str(e))
        result["errors"].append(err)
        processed += 1

      await asyncio.sleep(0.5)

    if processed >= limit:
      break
    if len(jobs) < 20:
      break
    await asyncio.sleep(1)

  # Update web_sources record
  try:
    async with SessionLocal() as s:
      ws = (await s.execute(
        select(WebSource).where(WebSource.slug == SOURCE_SLUG)
      )).scalar_one_or_none()
      if ws:
        count = (await s.execute(
          select(sqla_func.count(Vacancy.id)).where(Vacancy.source_channel == SOURCE_CHANNEL)
        )).scalar() or 0
        ws.last_synced_at = datetime.now(timezone.utc)
        ws.vacancies_count = count
        await s.commit()
  except Exception:
    pass

  await persist_parser_log(
    level="INFO",
    event="web_sync_complete",
    message_en=f"Web sync {SOURCE_SLUG}: +{result['added']} added, {result['skipped']} skipped, {len(result['errors'])} errors",
  )

  return result


async def ensure_source_record():
  """Create the web_sources row for degencryptojobs if it doesn't exist."""
  async with SessionLocal() as s:
    existing = (await s.execute(
      select(WebSource).where(WebSource.slug == SOURCE_SLUG)
    )).scalar_one_or_none()
    if not existing:
      ws = WebSource(
        slug=SOURCE_SLUG,
        name="DegenCryptoJobs",
        url="https://degencryptojobs.com",
        parser_type="degencryptojobs",
        enabled=True,
        sync_interval_minutes=180,
        max_pages=1,
      )
      s.add(ws)
      await s.commit()
