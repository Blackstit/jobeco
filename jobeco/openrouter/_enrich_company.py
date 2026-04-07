"""Company profile enrichment via Perplexity (through OpenRouter)."""
from __future__ import annotations

import json as _json
import re

import httpx
import structlog

from jobeco.runtime_settings import get_runtime_settings
from jobeco.processing.company_branding import brand_favicon_url

_log = structlog.get_logger()


async def enrich_company_profile(
  company_name: str | None,
  company_url: str | None = None,
) -> dict:
  """
  Use Perplexity (via OpenRouter) to find public info about a company.

  Returns dict with keys:
    summary, industry, size, founded, website, headquarters, logo_url
  """
  if not company_name or len(company_name.strip()) < 2:
    return {}

  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return {}

  name = company_name.strip()
  api_url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

  context = f"Company name: {name}"
  if company_url:
    context += f"\nKnown website: {company_url}"

  system = (
    "You are a concise company research assistant. Given a company name, "
    "find publicly available information and return ONLY valid JSON.\n"
    "ALL output MUST be in ENGLISH.\n\n"
    "Return JSON:\n"
    "{\n"
    '  "summary": "2-3 sentence description of what the company does, their product/market.",\n'
    '  "industry": "primary industry/vertical, e.g. iGaming, FinTech, AI, GameDev, Marketing, Web3, Crypto, DeFi, NFT, DAO, GameFi, RWA, L1/L2, Design, E-commerce, SaaS, etc. String or null.",\n'
    '  "size": "approximate employee count like 10-50, 50-200, 200-1000, 1000+ or null",\n'
    '  "founded": "year as string or null",\n'
    '  "website": "official website URL or null (only if different from known)",\n'
    '  "headquarters": "city, country or null",\n'
    '  "socials": {"linkedin": "url or null", "twitter": "url or null", "facebook": "url or null", '
    '"instagram": "url or null", "telegram": "url or null", "github": "url or null", "youtube": "url or null"}\n'
    "}\n\n"
    "For socials: only include platforms where you found an actual official link. Set others to null.\n"
    "If you cannot find any information about this company, return: {\"summary\": null}\n"
    "Do NOT invent information. Only state facts you are confident about."
  )

  model = runtime.get("openrouter", {}).get("model_perplexity") or "perplexity/sonar"

  req_payload = {
    "model": model,
    "messages": [
      {"role": "system", "content": system},
      {"role": "user", "content": context},
    ],
    "temperature": 0.0,
    "max_tokens": 700,
  }

  try:
    async with httpx.AsyncClient(timeout=30) as client:
      r = await client.post(api_url, headers=headers, json=req_payload)
      r.raise_for_status()
      raw_content = r.json()["choices"][0]["message"]["content"]

    raw = (raw_content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    data = None
    try:
      data = _json.loads(raw)
    except Exception:
      start, end = raw.find("{"), raw.rfind("}")
      if start != -1 and end > start:
        data = _json.loads(raw[start:end + 1])

    if not isinstance(data, dict) or not data.get("summary"):
      return {}

    result: dict = {}
    for key in ("summary", "industry", "size", "founded", "website", "headquarters"):
      v = data.get(key)
      if v and isinstance(v, str) and v.strip().lower() not in ("null", "none", "n/a", "unknown"):
        result[key] = v.strip()

    # Parse social links
    raw_socials = data.get("socials") or {}
    if isinstance(raw_socials, dict):
      clean_socials = {}
      for sk in ("linkedin", "twitter", "facebook", "instagram", "telegram", "github", "youtube"):
        sv = raw_socials.get(sk)
        if sv and isinstance(sv, str) and sv.strip().lower() not in ("null", "none", "n/a", "") and sv.strip().startswith("http"):
          clean_socials[sk] = sv.strip()
      if clean_socials:
        result["socials"] = clean_socials

    # Favicon only from a real corporate domain (not ATS / job boards).
    logo_url = None
    if result.get("website"):
      logo_url = brand_favicon_url(result["website"])
    if not logo_url and company_url:
      logo_url = brand_favicon_url(company_url)
    if logo_url:
      result["logo_url"] = logo_url

    return result
  except Exception as exc:
    _log.warning("company_profile_enrichment_error", company=name, error=str(exc))
    return {}
