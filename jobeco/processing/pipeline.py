from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text
from telethon import events

from jobeco.db.session import SessionLocal
from jobeco.db.models import Vacancy, ParserLog, Company

COMPANY_CACHE_TTL_DAYS = 90
from jobeco.openrouter.client import analyze_with_openrouter, embed_text, prevalidate_post, score_vacancy_with_openrouter, resolve_company_info, enrich_company_profile
from jobeco.settings import settings
from jobeco.runtime_settings import get_runtime_settings
from jobeco.processing.normalization import normalize_vacancy_fields
from jobeco.processing.company_branding import brand_favicon_url, sanitize_logo_url, pick_corporate_website, is_ats_or_job_board_url
from jobeco.processing.ats_enricher import fetch_ats_description

log = structlog.get_logger()



async def try_enrich_from_ats(raw_text: str, apply_url: str | None) -> str:
  """
  If the raw_text is short and apply_url points to a known ATS,
  fetch the full job description and return enriched text.
  Falls back to original raw_text on any failure.
  """
  if not apply_url or not apply_url.startswith("http"):
    return raw_text
  if len(raw_text) > 3000:
    return raw_text
  try:
    ats_text = await fetch_ats_description(apply_url)
    if ats_text and len(ats_text) > len(raw_text) * 0.5:
      split_desc = "\nDescription:\n"
      split_full = "\nFull Job Description"
      enriched = raw_text.split(split_desc)[0].split(split_full)[0]
      enriched = enriched.rstrip() + "\n\nFull Job Description (from company ATS):\n" + ats_text
      # Always keep apply_url visible so LLM extracts it into contacts
      if apply_url and apply_url not in enriched:
        enriched += f"\n\nApply: {apply_url}"
      log.info("ats_enriched_pipeline", url=apply_url[:80], original_len=len(raw_text), enriched_len=len(enriched))
      return enriched
  except Exception as exc:
    log.debug("ats_enrich_failed", url=apply_url[:80], error=str(exc))
  return raw_text


def _extract_entity_urls(msg) -> list[str]:
  """Extract URLs hidden in Telegram hypertext entities (MessageEntityTextUrl)."""
  urls: list[str] = []
  entities = getattr(msg, "entities", None) or []
  for ent in entities:
    url = getattr(ent, "url", None)
    if url and isinstance(url, str) and url.startswith("http"):
      urls.append(url.strip())
  return urls

_FORM_URL_RE = re.compile(
  r'https?://(?:'
  r'forms\.gle/[A-Za-z0-9]+|'
  r'docs\.google\.com/forms/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.typeform\.com/[^\s)\"\'<>]+|'
  r'(?:www\.)?jotform\.com/[^\s)\"\'<>]+|'
  r'tally\.so/[^\s)\"\'<>]+|'
  r'airtable\.com/shr[^\s)\"\'<>]+|'
  r'(?:www\.)?surveymonkey\.com/[^\s)\"\'<>]+|'
  r'jobs\.lever\.co/[^\s)\"\'<>]+|'
  r'boards\.greenhouse\.io/[^\s)\"\'<>]+|'
  r'apply\.workable\.com/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.breezy\.hr/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.bamboohr\.com/[^\s)\"\'<>]+|'
  r'jobs\.smartrecruiters\.com/[^\s)\"\'<>]+|'
  r'jobs\.ashbyhq\.com/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.recruitee\.com/[^\s)\"\'<>]+'
  r')',
  re.IGNORECASE,
)


_AGGREGATOR_DOMAINS = [
  "jaabz.com", "indeed.com", "glassdoor.com", "hh.ru", "rabota.ua",
  "work.ua", "djinni.co", "jooble.org", "careerjet.com", "simplyhired.com",
  "ziprecruiter.com", "monster.com",
]


def _strip_channel_from_contacts(contacts: list[str], channel_username: str | None) -> list[str]:
  """Remove the source Telegram channel from contacts — it's not an apply contact."""
  if not channel_username:
    return contacts
  slug = channel_username.lower().lstrip("@")
  result = []
  for c in contacts:
    lc = (c or "").lower().lstrip("@")
    # Drop exact @channel match or t.me/channel URLs
    if lc == slug:
      continue
    if f"t.me/{slug}" in lc:
      continue
    result.append(c)
  return result


def _merge_entity_contacts(contacts: list[str], entity_urls: list[str]) -> list[str]:
  """Merge hypertext entity URLs into contacts, filtering aggregator self-links."""
  existing = {c.lower() for c in contacts}
  for url in entity_urls:
    lc = url.lower()
    if lc in existing:
      continue
    if any(agg in lc for agg in _AGGREGATOR_DOMAINS):
      continue
    contacts.append(url)
    existing.add(lc)
  return contacts


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


def _resolve_company_logo_url(
  company_profile: dict,
  company_url: str | None,
  existing_website: str | None = None,
) -> str | None:
  """Prefer explicit/sanitized profile logo, then favicon from a non-ATS corporate website."""
  explicit = sanitize_logo_url((company_profile or {}).get("logo_url"))
  if explicit:
    return explicit
  return brand_favicon_url(company_url or (company_profile or {}).get("website") or existing_website)


_INDUSTRY_TO_DOMAIN_INDUSTRY = {
  "igaming": "igaming", "gambling": "igaming", "casino": "igaming", "betting": "igaming",
  "fintech": "fintech", "financial services": "fintech", "banking": "fintech",
  "blockchain": "web3", "cryptocurrency": "crypto", "web3": "web3", "crypto": "crypto",
  "defi": "defi", "decentralized finance": "defi",
  "nft": "nft", "non-fungible": "nft",
  "dao": "dao", "decentralized autonomous": "dao",
  "gamefi": "gamefi", "play-to-earn": "gamefi", "p2e": "gamefi",
  "rwa": "rwa", "real world asset": "rwa", "tokenization": "rwa",
  "layer 1": "l1l2", "layer 2": "l1l2", "l1": "l1l2", "l2": "l1l2", "rollup": "l1l2",
  "game development": "gaming", "gamedev": "gaming", "esports": "gaming", "video game": "gaming",
  "artificial intelligence": "ai", "machine learning": "ai",
  "marketing": "marketing", "advertising": "marketing", "digital marketing": "marketing",
  "design": "design", "graphic design": "design",
}
_INDUSTRY_TO_DOMAIN_SUMMARY = {
  "igaming": "igaming", "online casino": "igaming", "sports betting": "igaming",
  "fintech": "fintech",
  "blockchain": "web3", "cryptocurrency": "crypto", "web3": "web3", "crypto": "crypto",
  "defi": "defi", "decentralized finance": "defi", "dex": "defi", "lending protocol": "defi", "yield": "defi",
  "nft": "nft", "non-fungible": "nft", "collectible": "nft", "opensea": "nft",
  "dao": "dao", "governance": "dao", "decentralized autonomous": "dao",
  "gamefi": "gamefi", "play-to-earn": "gamefi", "play to earn": "gamefi", "p2e": "gamefi",
  "rwa": "rwa", "real world asset": "rwa", "tokeniz": "rwa",
  "layer 2": "l1l2", "layer 1": "l1l2", "rollup": "l1l2", "zk-rollup": "l1l2", "optimistic rollup": "l1l2",
  "artificial intelligence": "ai", "machine learning": "ai",
}


async def get_cached_company_profile(
  company_name: str | None,
  *,
  max_age_days: int | None = None,
) -> dict | None:
  """
  Return an existing company's profile in the same shape as enrich_company_profile(),
  or None if we should call Perplexity (no row / stale / missing core fields / cache disabled).

  Reads `company_cache.enabled` and `company_cache.ttl_days` from runtime settings,
  so the cache can be tuned or disabled live from the admin UI.
  """
  if not company_name or len(company_name.strip()) < 2:
    return None

  runtime = await get_runtime_settings()
  cache_cfg = runtime.get("company_cache") or {}
  if not cache_cfg.get("enabled", True):
    return None
  if max_age_days is None:
    max_age_days = int(cache_cfg.get("ttl_days", COMPANY_CACHE_TTL_DAYS))

  name_lc = company_name.strip().lower()
  try:
    async with SessionLocal() as s:
      from sqlalchemy import select
      row = (await s.execute(select(Company).where(Company.name_lower == name_lc))).scalar_one_or_none()
  except Exception:
    log.exception("company_cache_lookup_failed", company=company_name)
    return None

  if not row:
    return None
  # Need at least the two most useful fields to consider cache usable.
  if not row.summary or not row.industry:
    return None
  # TTL: re-enrich if last update is too old, so we can pick up new socials/size/etc.
  if row.updated_at is not None:
    now = datetime.now(timezone.utc)
    try:
      age = now - row.updated_at
    except TypeError:
      age = now - row.updated_at.replace(tzinfo=timezone.utc)
    if age > timedelta(days=max_age_days):
      return None

  return {
    "summary": row.summary,
    "industry": row.industry,
    "size": row.size,
    "founded": row.founded,
    "website": row.website,
    "headquarters": row.headquarters,
    "socials": row.socials or {},
    "logo_url": row.logo_url,
  }


async def upsert_company(
  company_name: str | None,
  company_profile: dict | None = None,
  company_url: str | None = None,
  company_linkedin: str | None = None,
) -> int | None:
  """Create or update a Company row, return its id."""
  if not company_name or len(company_name.strip()) < 2:
    return None

  name = company_name.strip()
  name_lc = name.lower()
  cp = company_profile or {}

  inferred_domains: list[str] = []
  _CRYPTO_SUBS = {"defi", "nft", "dao", "gamefi", "rwa", "l1l2"}
  industry_lc = (cp.get("industry") or "").lower()
  summary_lc = (cp.get("summary") or "").lower()
  for keyword, domain in _INDUSTRY_TO_DOMAIN_INDUSTRY.items():
    if keyword in industry_lc and domain not in inferred_domains:
      inferred_domains.append(domain)
  for keyword, domain in _INDUSTRY_TO_DOMAIN_SUMMARY.items():
    if keyword in summary_lc and domain not in inferred_domains:
      inferred_domains.append(domain)

  # Ensure umbrella domains: if any crypto sub-vertical found, add 'web3' and/or 'crypto'
  if any(d in _CRYPTO_SUBS for d in inferred_domains):
    if "web3" not in inferred_domains:
      inferred_domains.append("web3")
    if "crypto" not in inferred_domains:
      inferred_domains.append("crypto")

  try:
    async with SessionLocal() as s:
      from sqlalchemy import select
      existing = (await s.execute(select(Company).where(Company.name_lower == name_lc))).scalar_one_or_none()
      if existing:
        if cp.get("summary") and not existing.summary:
          existing.summary = cp["summary"]
        if cp.get("industry") and not existing.industry:
          existing.industry = cp["industry"]
        if cp.get("size") and not existing.size:
          existing.size = cp["size"]
        if cp.get("founded") and not existing.founded:
          existing.founded = cp["founded"]
        if cp.get("headquarters") and not existing.headquarters:
          existing.headquarters = cp["headquarters"]
        old_website = existing.website
        best_web = pick_corporate_website(company_url, cp.get("website"))
        if best_web:
          existing.website = best_web
        elif existing.website and is_ats_or_job_board_url(existing.website):
          existing.website = None
        new_logo = _resolve_company_logo_url(cp, company_url, existing.website)
        website_improved = best_web and (old_website != best_web)
        should_refresh_logo = (
          website_improved
          or not existing.logo_url
          or sanitize_logo_url(existing.logo_url) is None
        )
        if should_refresh_logo:
          existing.logo_url = new_logo
        if company_linkedin and not existing.linkedin:
          existing.linkedin = company_linkedin
        # Merge socials
        new_socials = cp.get("socials") or {}
        if new_socials:
          old_socials = existing.socials or {}
          merged = {**old_socials, **{k: v for k, v in new_socials.items() if v and not old_socials.get(k)}}
          existing.socials = merged
        old_domains = set(existing.domains or [])
        for d in inferred_domains:
          old_domains.add(d)
        existing.domains = list(old_domains)
        await s.commit()
        return int(existing.id)
      else:
        website_val = pick_corporate_website(company_url, cp.get("website"))
        logo_val = _resolve_company_logo_url(cp, company_url, website_val)
        c = Company(
          name=name,
          name_lower=name_lc,
          website=website_val,
          linkedin=company_linkedin,
          logo_url=logo_val,
          summary=cp.get("summary"),
          industry=cp.get("industry"),
          size=cp.get("size"),
          founded=cp.get("founded"),
          headquarters=cp.get("headquarters"),
          domains=inferred_domains,
          socials=cp.get("socials") or {},
        )
        s.add(c)
        await s.flush()
        cid = int(c.id)
        await s.commit()
        return cid
  except Exception:
    log.exception("upsert_company_failed", company=name)
    return None


def _boost_company_score(scoring: dict, company_profile: dict, company_info: dict) -> dict:
  """If we enriched the company externally, bump the company_profile criterion."""
  if not company_profile or not company_profile.get("summary"):
    return scoring

  results = scoring.get("scoring_results") or []
  has_website = bool(pick_corporate_website(company_info.get("company_url"), company_profile.get("website")))
  has_summary = bool(company_profile.get("summary"))

  new_score = 7
  if has_website and has_summary:
    new_score = 8
  if company_profile.get("industry"):
    new_score = min(new_score + 1, 9)

  for r in results:
    if r.get("key") == "company_profile":
      old = r.get("score", 0)
      if new_score > old:
        r["score"] = new_score
        r["summary"] = "Company verified via external enrichment: " + (company_profile.get("industry") or "known company") + "."
      break

  # Recalculate total_score
  weights = {
    "tasks_and_kpi": 0.30, "compensation_clarity": 0.25,
    "tech_stack_and_ops": 0.20, "requirement_logic": 0.15,
    "company_profile": 0.10,
  }
  total = 0.0
  for r in results:
    w = weights.get(r.get("key"), 0)
    total += r.get("score", 0) * w
  if results:
    scoring["total_score"] = round(total, 1)

  # Remove "no company info" from red_flags
  flags = scoring.get("red_flags") or []
  scoring["red_flags"] = [f for f in flags if "no company" not in f.lower()]

  return scoring


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


async def save_vacancy(payload: dict, embedding: list[float] | None) -> int | None:
  """Save a vacancy. Returns vacancy id, or None if no contacts could be extracted."""
  async with SessionLocal() as s:
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
      if channel_username and (lc == f"@{channel_username}" or channel_username in lc and "t.me/" in lc):
        continue
      if "subscribe" in lc or "подпис" in lc or "channel" in lc and "t.me/" in lc:
        continue
      if any(agg in lc for agg in _AGGREGATOR_DOMAINS):
        continue
      if lc in seen:
        continue
      seen.add(lc)
      clean_contacts.append(c_str)

    if not clean_contacts:
      log.info("vacancy_skipped_no_contacts", title=payload.get("title"))
      return None

    _norm_role, _norm_seniority = normalize_vacancy_fields(
      role=payload.get("role"),
      seniority=payload.get("seniority"),
      title=payload.get("title"),
      standardized_title=payload.get("standardized_title"),
    )

    v = Vacancy(
      tg_message_id=payload.get("tg_message_id"),
      tg_channel_id=payload.get("tg_channel_id"),
      tg_channel_username=payload.get("tg_channel_username"),
      source_url=payload.get("source_url"),
      company_name=payload.get("company_name"),
      company_url=payload.get("company_url"),
      title=payload.get("title"),
      location_type=(payload.get("location_type") or "").lower().strip() or None,
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
      domains=payload.get("domains", []),
      risk_label=payload.get("risk_label"),
      recruiter=payload.get("recruiter"),
      contacts=clean_contacts,
      description=payload.get("description"),
      responsibilities=payload.get("responsibilities"),
      requirements=payload.get("requirements"),
      conditions=payload.get("conditions"),
      role=_norm_role,
      seniority=_norm_seniority,
      english_level=payload.get("english_level"),
      standardized_title=payload.get("standardized_title"),
      language=payload.get("language"),
      company_id=payload.get("company_id"),
      external_id=payload.get("external_id"),
      source_channel=payload.get("source_channel"),
    )
    s.add(v)
    await s.flush()
    vacancy_id = int(v.id)
    await s.commit()
    return vacancy_id


async def process_message(event: events.NewMessage.Event) -> None:
  msg = event.message
  if not msg or not getattr(msg, "message", None):
    return

  text_raw = (msg.message or "").strip()
  if not text_raw:
    return

  # Extract URLs from hypertext entities (invisible in plain text).
  entity_urls = _extract_entity_urls(msg)
  if entity_urls:
    text_raw += "\n\n[Hyperlinks: " + " , ".join(entity_urls) + "]"
  _extracted_entity_contacts = list(entity_urls)  # will be merged into contacts later

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

  company_profile = {}
  cached_profile = await get_cached_company_profile(analysis.get("company_name"))
  if cached_profile is not None:
    company_profile = cached_profile
    await persist_parser_log(
      level="INFO",
      event="company_cache_hit",
      message_en=f"Company profile served from cache: {analysis.get('company_name')}",
      channel_username=channel_username,
      tg_message_id=msg_id,
    )
  else:
    try:
      company_profile = await enrich_company_profile(
        company_name=analysis.get("company_name"),
        company_url=company_info.get("company_url"),
      )
    except Exception:
      log.warning("company_profile_enrichment_failed", company=analysis.get("company_name"))

  # Adjust company_profile criterion in scoring if enrichment found data
  scoring = _boost_company_score(scoring, company_profile, company_info)
  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    pass
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  company_id = await upsert_company(
    company_name=analysis.get("company_name"),
    company_profile=company_profile,
    company_url=company_info.get("company_url"),
    company_linkedin=company_info.get("company_linkedin"),
  )
  vacancy_domains = [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()]
  if company_id:
    try:
      async with SessionLocal() as s:
        from sqlalchemy import select
        comp = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if comp and comp.domains:
          for cd in comp.domains:
            if cd and cd.lower() not in vacancy_domains:
              vacancy_domains.append(cd.lower())
    except Exception:
      pass

  payload = {
    "tg_message_id": msg.id,
    "tg_channel_id": getattr(event.chat, "id", None),
    "tg_channel_username": getattr(event.chat, "username", None),
    "source_url": source_url,
    "company_name": analysis.get("company_name"),
    "company_url": pick_corporate_website(company_info.get("company_url"), company_profile.get("website")),
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
      "company_profile": company_profile if company_profile else None,
    },
    "company_id": company_id,
    "domains": vacancy_domains,
    "risk_label": analysis.get("risk_label"),
    "recruiter": analysis.get("recruiter"),
    "contacts": _strip_channel_from_contacts(
      _merge_entity_contacts(
        _enrich_contacts_with_forms(analysis.get("contacts") or [], text_raw),
        _extracted_entity_contacts,
      ),
      channel_username,
    ),
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
  if vacancy_id is None:
    await persist_parser_log(
      level="INFO",
      event="vacancy_skipped",
      message_en="Vacancy skipped: no contacts extracted",
      channel_username=channel_username,
      tg_message_id=msg_id,
    )
    log.info("vacancy_skipped_no_contacts", msg_id=msg.id)
    return
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

  company_profile = {}
  cached_profile = await get_cached_company_profile(analysis.get("company_name"))
  if cached_profile is not None:
    company_profile = cached_profile
    await persist_parser_log(
      level="INFO",
      event="company_cache_hit",
      message_en=f"Company profile served from cache: {analysis.get('company_name')}",
      channel_username=tg_channel_username,
      tg_message_id=tg_message_id,
    )
  else:
    try:
      company_profile = await enrich_company_profile(
        company_name=analysis.get("company_name"),
        company_url=company_info.get("company_url"),
      )
    except Exception:
      pass

  scoring = _boost_company_score(scoring, company_profile, company_info)
  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    pass
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  company_id = await upsert_company(
    company_name=analysis.get("company_name"),
    company_profile=company_profile,
    company_url=company_info.get("company_url"),
    company_linkedin=company_info.get("company_linkedin"),
  )
  vacancy_domains = [str(x).lower() for x in (analysis.get("domains") or []) if str(x).strip()]
  if company_id:
    try:
      async with SessionLocal() as s:
        from sqlalchemy import select
        comp = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if comp and comp.domains:
          for cd in comp.domains:
            if cd and cd.lower() not in vacancy_domains:
              vacancy_domains.append(cd.lower())
    except Exception:
      pass

  payload = {
    "tg_message_id": tg_message_id,
    "tg_channel_id": tg_channel_id,
    "tg_channel_username": tg_channel_username,
    "source_url": source_url,
    "company_name": analysis.get("company_name"),
    "company_url": pick_corporate_website(company_info.get("company_url"), company_profile.get("website")),
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
      "company_profile": company_profile if company_profile else None,
    },
    "company_id": company_id,
    "domains": vacancy_domains,
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
  if vacancy_id is None:
    await persist_parser_log(
      level="INFO",
      event="vacancy_skipped",
      message_en="Vacancy skipped: no contacts extracted",
      channel_username=tg_channel_username,
      tg_message_id=tg_message_id,
    )
    return False
  await persist_parser_log(
    level="INFO",
    event="vacancy_added",
    message_en=f"Vacancy added. ID {vacancy_id}",
    channel_username=tg_channel_username,
    tg_message_id=tg_message_id,
    vacancy_id=vacancy_id,
  )
  return True
