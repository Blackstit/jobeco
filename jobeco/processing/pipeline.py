from __future__ import annotations

import re
from datetime import datetime

import structlog
from sqlalchemy import text
from telethon import events

from jobeco.db.session import SessionLocal
from jobeco.db.models import Vacancy, ParserLog
from jobeco.openrouter.client import analyze_with_openrouter, embed_text, prevalidate_post, score_vacancy_with_openrouter, resolve_company_info
from jobeco.settings import settings
from jobeco.runtime_settings import get_runtime_settings

log = structlog.get_logger()

_FORM_URL_RE = re.compile(
  r'https?://(?:'
  r'forms\.gle/[A-Za-z0-9]+|'
  r'docs\.google\.com/forms/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.typeform\.com/[^\s)\"\'<>]+|'
  r'(?:www\.)?jotform\.com/[^\s)\"\'<>]+|'
  r'tally\.so/[^\s)\"\'<>]+|'
  r'airtable\.com/shr[^\s)\"\'<>]+|'
  r'(?:www\.)?surveymonkey\.com/[^\s)\"\'<>]+'
  r')',
  re.IGNORECASE,
)


def _enrich_contacts_with_forms(contacts: list[str], raw_text: str | None) -> list[str]:
  """Append application-form URLs found in raw_text that the LLM missed."""
  if not raw_text:
    return contacts
  existing_lower = {c.lower() for c in contacts}
  for m in _FORM_URL_RE.finditer(raw_text):
    url = m.group(0)
    if url.lower() not in existing_lower:
      contacts.append(url)
      existing_lower.add(url.lower())
  return contacts


async def persist_parser_log(
  *,
  level: str,
  event: str,
  message_en: str,
  channel_username: str | None = None,
  tg_message_id: int | None = None,
  vacancy_id: int | None = None,
  extra: dict | None = None,
) -> None:
  """
  Store parser lifecycle events for the admin UI.

  This is intentionally "best effort": logging must not break ingestion.
  """
  try:
    async with SessionLocal() as s:
      s.add(
        ParserLog(
          level=level,
          event=event,
          message_en=message_en,
          channel_username=channel_username,
          tg_message_id=tg_message_id,
          vacancy_id=vacancy_id,
          extra=extra or {},
        )
      )
      await s.commit()
  except Exception:
    # Never block parsing due to logging issues, but make it visible in container logs.
    log.exception("parser_log_failed", event=event, level=level)
    return


async def is_duplicate(embedding: list[float]) -> bool:
  # cosine distance in pgvector: <=> (smaller is closer)
  # similarity = 1 - distance
  vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
  q = text(
    """
    SELECT 1
    FROM vacancies
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> (:vec)::vector
    LIMIT 1
    """
  )
  async with SessionLocal() as s:
    row = (await s.execute(q, {"vec": vec})).first()
    if not row:
      return False
    # compute similarity from distance in a second query to avoid extra select:
    q2 = text(
      """
      SELECT 1 - (embedding <=> (:vec)::vector) as sim
      FROM vacancies
      WHERE embedding IS NOT NULL
      ORDER BY embedding <=> (:vec)::vector
      LIMIT 1
      """
    )
    sim = (await s.execute(q2, {"vec": vec})).scalar()
    runtime = await get_runtime_settings()
    threshold = float(runtime.get("parser", {}).get("dedup_threshold", settings.dedup_threshold))
    return bool(sim is not None and float(sim) >= threshold)


async def save_vacancy(payload: dict, embedding: list[float] | None) -> int:
  async with SessionLocal() as s:
    # postprocess contacts: dedupe + remove obvious footer/channel contacts
    contacts = payload.get("contacts", []) or []
    clean_contacts: list[str] = []
    seen = set()
    channel_username = (payload.get("tg_channel_username") or "").lstrip("@").lower()
    for c in contacts:
      if not c:
        continue
      c_str = str(c).strip()
      if not c_str:
        continue
      lc = c_str.lower()
      # drop generic channel footer links/usernames
      if channel_username and (lc == f"@{channel_username}" or channel_username in lc and "t.me/" in lc):
        continue
      if "subscribe" in lc or "подпис" in lc or "channel" in lc and "t.me/" in lc:
        continue
      if lc in seen:
        continue
      seen.add(lc)
      clean_contacts.append(c_str)

    v = Vacancy(
      tg_message_id=payload.get("tg_message_id"),
      tg_channel_id=payload.get("tg_channel_id"),
      tg_channel_username=payload.get("tg_channel_username"),
      source_url=payload.get("source_url"),
      company_name=payload.get("company_name"),
      title=payload.get("title"),
      location_type=payload.get("location_type"),
      salary_min_usd=payload.get("salary_min_usd"),
      salary_max_usd=payload.get("salary_max_usd"),
      stack=payload.get("stack", []),
      category=payload.get("category"),
      ai_score_value=payload.get("ai_score_value"),
      summary_ru=payload.get("summary_ru"),
      summary_en=payload.get("summary_en"),
      raw_text=payload.get("raw_text"),
      metadata_json=payload.get("metadata", {}),
      embedding=embedding,
      # new fields (if present)
      domains=payload.get("domains", []),
      risk_label=payload.get("risk_label"),
      recruiter=payload.get("recruiter"),
      contacts=clean_contacts,
      description=payload.get("description"),
      responsibilities=payload.get("responsibilities"),
      requirements=payload.get("requirements"),
      conditions=payload.get("conditions"),
      role=payload.get("role"),
      seniority=payload.get("seniority"),
      english_level=payload.get("english_level"),
      standardized_title=payload.get("standardized_title"),
      language=payload.get("language"),
    )
    s.add(v)
    await s.flush()
    vacancy_id = int(v.id)  # assigned after flush
    await s.commit()
    return vacancy_id


async def process_message(event: events.NewMessage.Event) -> None:
  msg = event.message
  if not msg or not getattr(msg, "message", None):
    return

  text_raw = (msg.message or "").strip()
  if not text_raw:
    return

  # Best-effort source URL so public API doesn't return `null` for Telethon-ingested rows.
  source_url: str | None = None
  try:
    chat_username = getattr(event.chat, "username", None)
    if chat_username and getattr(msg, "id", None):
      uname = str(chat_username).lstrip("@")
      source_url = f"https://t.me/{uname}/{msg.id}"
  except Exception:
    source_url = None

  channel_username = getattr(event.chat, "username", None)
  msg_id = getattr(msg, "id", None)
  if channel_username:
    channel_username = str(channel_username).lstrip("@")

  await persist_parser_log(
    level="INFO",
    event="post_detected",
    message_en=f"Detected a post in channel @{channel_username}" if channel_username else "Detected a post in a channel",
    channel_username=channel_username,
    tg_message_id=msg_id,
  )

  # Prevalidation (cheap) - skip non-vacancy content
  pv = await prevalidate_post(text_raw)
  if not pv.get("is_vacancy", True):
    log.info("prevalidate_skip", msg_id=msg.id, content_type=pv.get("content_type"), reason=pv.get("reason"))
    await persist_parser_log(
      level="WARNING",
      event="prevalidate_skip",
      message_en=f"Post skipped: not a vacancy (reason={pv.get('reason') or 'unknown'})",
      channel_username=channel_username,
      tg_message_id=msg_id,
      extra={"content_type": pv.get("content_type")},
    )
    return

  await persist_parser_log(
    level="INFO",
    event="post_passed_prevalidation",
    message_en="Post passed pre-validation",
    channel_username=channel_username,
    tg_message_id=msg_id,
  )

  embedding = await embed_text(text_raw)
  if embedding and await is_duplicate(embedding):
    log.info("dedup_skip", msg_id=msg.id)
    await persist_parser_log(
      level="INFO",
      event="dedup_skip",
      message_en="Post skipped: duplicate detected",
      channel_username=channel_username,
      tg_message_id=msg_id,
    )
    return

  analysis = await analyze_with_openrouter(text_raw)

  # Enrich with verified company web presence (before scoring so heuristic can use it).
  company_info = await resolve_company_info(
    company_name=analysis.get("company_name"),
    raw_text=text_raw,
    llm_website=analysis.get("company_website"),
    llm_linkedin=analysis.get("company_linkedin"),
  )
  analysis["_company_url_verified"] = company_info.get("company_url_verified", False)
  analysis["_company_linkedin_verified"] = company_info.get("company_linkedin_verified", False)

  try:
    scoring = await score_vacancy_with_openrouter(text_raw, analysis)
  except Exception:
    scoring = {"total_score": 5.0, "scoring_results": [], "overall_summary": "", "red_flags": []}

  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    ai_score_value_0_10 = int(analysis.get("ai_score_value") or 5)
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  payload = {
    "tg_message_id": msg.id,
    "tg_channel_id": getattr(event.chat, "id", None),
    "tg_channel_username": getattr(event.chat, "username", None),
    "source_url": source_url,
    "company_name": analysis.get("company_name"),
    "company_url": company_info.get("company_url"),
    "title": analysis.get("title"),
    "location_type": analysis.get("location_type"),
    "salary_min_usd": analysis.get("salary_min_usd"),
    "salary_max_usd": analysis.get("salary_max_usd"),
    "stack": analysis.get("stack", []),
    "category": analysis.get("category"),
    "ai_score_value": ai_score_value_0_10,
    "summary_ru": analysis.get("summary_ru"),
    "summary_en": analysis.get("summary_en"),
    "raw_text": text_raw,
    "metadata": {
      **(analysis.get("metadata", {}) or {}),
      "scoring": scoring,
      "company_linkedin": company_info.get("company_linkedin"),
      "company_url_verified": company_info.get("company_url_verified", False),
      "company_linkedin_verified": company_info.get("company_linkedin_verified", False),
      "employment_type": analysis.get("employment_type"),
      "language_requirements": analysis.get("language_requirements"),
    },
    "domains": [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()],
    "risk_label": analysis.get("risk_label"),
    "recruiter": analysis.get("recruiter"),
    "contacts": _enrich_contacts_with_forms(analysis.get("contacts") or [], text_raw),
    "description": analysis.get("description"),
    "responsibilities": analysis.get("responsibilities"),
    "requirements": analysis.get("requirements"),
    "conditions": analysis.get("conditions"),
    "role": analysis.get("role"),
    "seniority": (analysis.get("seniority") or "").lower().strip() or None,
    "english_level": (analysis.get("english_level") or "").strip().upper() or None,
    "standardized_title": analysis.get("standardized_title"),
    "language": analysis.get("language") or pv.get("language"),
    "created_at": datetime.utcnow().isoformat(),
  }
  vacancy_id = await save_vacancy(payload, embedding)
  await persist_parser_log(
    level="INFO",
    event="vacancy_added",
    message_en=f"Vacancy added. ID {vacancy_id}",
    channel_username=channel_username,
    tg_message_id=msg_id,
    vacancy_id=vacancy_id,
  )
  log.info("vacancy_saved", msg_id=msg.id, domains=analysis.get("domains"), risk_label=analysis.get("risk_label"))


async def process_text_message(
  *,
  text_raw: str,
  tg_message_id: int | None,
  tg_channel_id: int | None,
  tg_channel_username: str | None,
  source_url: str | None,
) -> bool:
  """
  Process a raw text message (used by admin-web 'fetch last 5').
  Returns True if vacancy saved, False if skipped.
  """
  text_raw = (text_raw or "").strip()
  if not text_raw:
    return False

  await persist_parser_log(
    level="INFO",
    event="post_detected",
    message_en=f"Detected a post in channel @{tg_channel_username}" if tg_channel_username else "Detected a post in a channel",
    channel_username=tg_channel_username,
    tg_message_id=tg_message_id,
  )

  pv = await prevalidate_post(text_raw)
  if not pv.get("is_vacancy", True):
    await persist_parser_log(
      level="WARNING",
      event="prevalidate_skip",
      message_en=f"Post skipped: not a vacancy (reason={pv.get('reason') or 'unknown'})",
      channel_username=tg_channel_username,
      tg_message_id=tg_message_id,
      extra={"content_type": pv.get("content_type")},
    )
    return False

  await persist_parser_log(
    level="INFO",
    event="post_passed_prevalidation",
    message_en="Post passed pre-validation",
    channel_username=tg_channel_username,
    tg_message_id=tg_message_id,
  )

  embedding = await embed_text(text_raw)
  if embedding and await is_duplicate(embedding):
    return False

  analysis = await analyze_with_openrouter(text_raw)

  # Enrich with verified company web presence (before scoring so heuristic can use it).
  company_info = await resolve_company_info(
    company_name=analysis.get("company_name"),
    raw_text=text_raw,
    llm_website=analysis.get("company_website"),
    llm_linkedin=analysis.get("company_linkedin"),
  )
  analysis["_company_url_verified"] = company_info.get("company_url_verified", False)
  analysis["_company_linkedin_verified"] = company_info.get("company_linkedin_verified", False)

  try:
    scoring = await score_vacancy_with_openrouter(text_raw, analysis)
  except Exception:
    scoring = {"total_score": 5.0, "scoring_results": [], "overall_summary": "", "red_flags": []}

  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    ai_score_value_0_10 = int(analysis.get("ai_score_value") or 5)
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  payload = {
    "tg_message_id": tg_message_id,
    "tg_channel_id": tg_channel_id,
    "tg_channel_username": tg_channel_username,
    "source_url": source_url,
    "company_name": analysis.get("company_name"),
    "company_url": company_info.get("company_url"),
    "title": analysis.get("title"),
    "location_type": analysis.get("location_type"),
    "salary_min_usd": analysis.get("salary_min_usd"),
    "salary_max_usd": analysis.get("salary_max_usd"),
    "stack": analysis.get("stack", []),
    "category": analysis.get("category"),
    "ai_score_value": ai_score_value_0_10,
    "summary_ru": analysis.get("summary_ru"),
    "summary_en": analysis.get("summary_en"),
    "raw_text": text_raw,
    "metadata": {
      **(analysis.get("metadata", {}) or {}),
      "scoring": scoring,
      "company_linkedin": company_info.get("company_linkedin"),
      "company_url_verified": company_info.get("company_url_verified", False),
      "company_linkedin_verified": company_info.get("company_linkedin_verified", False),
      "employment_type": analysis.get("employment_type"),
      "language_requirements": analysis.get("language_requirements"),
    },
    "domains": [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()],
    "risk_label": analysis.get("risk_label"),
    "recruiter": analysis.get("recruiter"),
    "contacts": _enrich_contacts_with_forms(analysis.get("contacts") or [], text_raw),
    "description": analysis.get("description"),
    "responsibilities": analysis.get("responsibilities"),
    "requirements": analysis.get("requirements"),
    "conditions": analysis.get("conditions"),
    "role": analysis.get("role"),
    "seniority": (analysis.get("seniority") or "").lower().strip() or None,
    "english_level": (analysis.get("english_level") or "").strip().upper() or None,
    "standardized_title": analysis.get("standardized_title"),
    "language": analysis.get("language") or pv.get("language"),
    "created_at": datetime.utcnow().isoformat(),
  }
  vacancy_id = await save_vacancy(payload, embedding)
  await persist_parser_log(
    level="INFO",
    event="vacancy_added",
    message_en=f"Vacancy added. ID {vacancy_id}",
    channel_username=tg_channel_username,
    tg_message_id=tg_message_id,
    vacancy_id=vacancy_id,
  )
  return True
