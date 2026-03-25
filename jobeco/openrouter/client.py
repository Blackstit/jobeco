from __future__ import annotations

import json as _json
import os
import re
from urllib.parse import urlparse

import httpx
import structlog

from jobeco.settings import settings
from jobeco.runtime_settings import get_runtime_settings

_log = structlog.get_logger()

_SKIP_DOMAINS = frozenset({
  "t.me", "telegram.me", "forms.gle", "docs.google.com",
  "bit.ly", "tinyurl.com", "goo.gl", "youtu.be", "youtube.com",
  "twitter.com", "x.com", "instagram.com", "facebook.com",
  "tiktok.com", "vk.com", "wa.me", "whatsapp.com",
})

_LINKEDIN_RE = re.compile(
  r'https?://(?:www\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9._%-]+/?',
  re.IGNORECASE,
)

_URL_RE = re.compile(r'https?://[^\s)<>\"\']+', re.IGNORECASE)


async def _head_check(url: str, *, timeout: float = 8.0) -> bool:
  """Return True if URL responds with a non-error status (allows redirects)."""
  try:
    async with httpx.AsyncClient(
      timeout=timeout, follow_redirects=True, verify=False,
    ) as client:
      r = await client.head(url, headers={"User-Agent": "Mozilla/5.0 JobEco/1.0"})
      return r.status_code < 400
  except Exception:
    return False


def _extract_candidate_urls(raw_text: str) -> list[str]:
  """Pull all http(s) URLs from text, dedup and return."""
  seen: set[str] = set()
  result: list[str] = []
  for m in _URL_RE.finditer(raw_text or ""):
    url = m.group(0).rstrip(".,;:!?)")
    low = url.lower()
    if low not in seen:
      seen.add(low)
      result.append(url)
  return result


def _is_company_site_candidate(url: str) -> bool:
  """Heuristic: could this URL be a company's own website?"""
  try:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
  except Exception:
    return False
  if not host:
    return False
  base = host.removeprefix("www.")
  if base in _SKIP_DOMAINS or any(base.endswith("." + sd) for sd in _SKIP_DOMAINS):
    return False
  if "linkedin.com" in base:
    return False
  if "google.com" in base or "typeform.com" in base or "jotform.com" in base:
    return False
  if "surveymonkey.com" in base or "airtable.com" in base or "tally.so" in base:
    return False
  return True


async def resolve_company_info(
  company_name: str | None,
  raw_text: str,
  llm_website: str | None = None,
  llm_linkedin: str | None = None,
) -> dict:
  """
  Enrich vacancy with verified company web presence.

  Strategy:
    1. Use URLs the LLM extracted from the post text (company_website, company_linkedin).
    2. Regex-extract additional URL candidates from raw_text.
    3. Verify each candidate with a HEAD request.

  Returns:
    {
      "company_url": str|None,          # verified company website
      "company_url_verified": bool,
      "company_linkedin": str|None,     # verified LinkedIn URL
      "company_linkedin_verified": bool,
      "all_urls_in_post": list[str],    # every URL found in raw text
    }
  """
  result: dict = {
    "company_url": None,
    "company_url_verified": False,
    "company_linkedin": None,
    "company_linkedin_verified": False,
    "all_urls_in_post": [],
  }

  all_urls = _extract_candidate_urls(raw_text)
  result["all_urls_in_post"] = all_urls

  # --- LinkedIn ---
  linkedin_candidates: list[str] = []
  if llm_linkedin and "linkedin.com" in llm_linkedin.lower():
    linkedin_candidates.append(llm_linkedin)
  for u in all_urls:
    if _LINKEDIN_RE.match(u) and u not in linkedin_candidates:
      linkedin_candidates.append(u)

  for candidate in linkedin_candidates[:3]:
    if await _head_check(candidate):
      result["company_linkedin"] = candidate
      result["company_linkedin_verified"] = True
      break
    else:
      result["company_linkedin"] = candidate
      result["company_linkedin_verified"] = False

  # --- Company Website ---
  website_candidates: list[str] = []
  if llm_website and _is_company_site_candidate(llm_website):
    website_candidates.append(llm_website)
  for u in all_urls:
    if _is_company_site_candidate(u) and u not in website_candidates:
      website_candidates.append(u)

  for candidate in website_candidates[:5]:
    if await _head_check(candidate):
      result["company_url"] = candidate
      result["company_url_verified"] = True
      break
    else:
      if not result["company_url"]:
        result["company_url"] = candidate
        result["company_url_verified"] = False

  _log.info(
    "company_resolve",
    company_name=company_name,
    website=result["company_url"],
    website_verified=result["company_url_verified"],
    linkedin=result["company_linkedin"],
    linkedin_verified=result["company_linkedin_verified"],
    urls_found=len(all_urls),
  )

  return result


async def prevalidate_post(text: str) -> dict:
  """
  Cheap prevalidation to filter out ads/info/memes/non-job posts before expensive analysis.
  Returns dict:
    - is_vacancy: bool
    - content_type: one of vacancy|ad|info|meme|other
    - confidence: float 0..1
    - reason: str
    - language: str|null (ru/en/other)
  If OPENROUTER_API_KEY is missing -> assume vacancy (do not block pipeline).
  """
  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return {
      "is_vacancy": True,
      "content_type": "vacancy",
      "confidence": 0.0,
      "reason": "OPENROUTER_API_KEY not set; skipping prevalidation",
      "language": None,
    }

  url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }

  default_system = """You are a strict JSON classifier for Telegram posts.
Decide if the text is a REAL job vacancy post. Filter out:
- ads/promotions, channel announcements, course sales
- informational posts, memes, quotes, community rules
- very short texts with no role/company/contacts

Return ONLY valid JSON (no markdown) with keys:
  is_vacancy: boolean
  content_type: "vacancy" | "ad" | "info" | "meme" | "other"
  confidence: number (0..1)
  reason: string (short)
  language: "ru" | "en" | "other" | null
"""
  system = runtime.get("prompts", {}).get("vacancy_prevalidate_system") or default_system

  payload = {
    "model": runtime["openrouter"]["model_classifier"],
    "messages": [
      {"role": "system", "content": system},
      {
        "role": "user",
        "content": text[: int(runtime.get("limits", {}).get("prevalidate_max_chars", 6000))],
      },
    ],
    "temperature": 0.0,
  }

  async with httpx.AsyncClient(timeout=30) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
  try:
    import json

    data = json.loads(content)
    # normalize minimal
    if not isinstance(data, dict):
      raise ValueError("not a dict")
    return {
      "is_vacancy": bool(data.get("is_vacancy")),
      "content_type": data.get("content_type") or "other",
      "confidence": float(data.get("confidence") or 0.0),
      "reason": str(data.get("reason") or ""),
      "language": data.get("language"),
    }
  except Exception:
    return {
      "is_vacancy": True,
      "content_type": "vacancy",
      "confidence": 0.0,
      "reason": "classifier_parse_failed",
      "language": None,
      "raw": content,
    }


async def embed_text(text: str) -> list[float] | None:
  """
  MVP: embeddings via OpenAI-compatible endpoint (many providers incl. OpenRouter support this style).
  If key is not set, returns None (pipeline will still save record without embedding).
  """
  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return None

  url = runtime["openrouter"]["base_url"].rstrip("/") + "/embeddings"
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  payload = {"model": settings.embedding_model, "input": text}
  async with httpx.AsyncClient(timeout=30) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    data = r.json()
    return data["data"][0]["embedding"]


async def analyze_with_openrouter(text: str) -> dict:
  """
  One-pass analysis returning structured dict.
  If OPENROUTER_API_KEY отсутствует — возвращаем простую заглушку.
  """
  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return {
      "domains": [],
      "risk_label": None,
      "ai_score_value": 5,
      "company_name": None,
      "title": None,
      "standardized_title": None,
      "role": None,
      "seniority": None,
      "location_type": None,
      "salary_min_usd": None,
      "salary_max_usd": None,
      "stack": [],
      "summary_ru": None,
      "summary_en": None,
      "recruiter": None,
      "contacts": [],
      "description": None,
      "responsibilities": None,
      "requirements": None,
      "conditions": None,
      "language": None,
      "metadata": {"note": "OPENROUTER_API_KEY not set; stub analysis"},
    }

  # System prompt for analyzer (kept inside function to avoid NameError and ensure consistency)
  default_system = (
    "You are a strict JSON generator. Analyze a Telegram job vacancy post and extract structured data.\n"
    "Return ONLY valid JSON (no markdown, no code blocks).\n"
    "\n"
    "IMPORTANT:\n"
    "- A vacancy can belong to MULTIPLE domains (e.g. ['ai','web3']).\n"
    "- ALL textual output fields MUST be in ENGLISH (even if input is RU). Do NOT output Russian.\n"
    "- summary_en MUST be short and useful for a listing card (max 300 chars).\n"
    "- description/responsibilities/requirements/conditions MUST be well-formatted Markdown text.\n"
    "  Output rules (anti-invention):\n"
    "  1) Use ONLY information that is explicitly present in the provided post text.\n"
    "  2) Do NOT invent missing responsibilities/requirements/conditions.\n"
    "  3) Paraphrase is allowed, but it must stay faithful to the source text.\n"
    "  4) If a list field is not supported by the text, return it as null.\n"
    "  5) If you are unsure whether a bullet is supported, omit it.\n"
    "  6) Prefer fewer, more accurate bullets over long fabricated lists.\n"
    "- contacts MUST include ALL ways to apply/respond: @username handles, emails, personal links,\n"
    "  AND application form URLs (Google Forms, Typeform, JotForm, etc.).\n"
    "  EXCLUDE only: channel footer links, \"subscribe\" links, and generic channel usernames.\n"
    "- company_website: if the post contains a URL to the company's own website or career page, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "- company_linkedin: if the post contains a LinkedIn company/profile URL, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "\n"
    "Required keys:\n"
    "- title: string|null\n"
    "- standardized_title: string|null (normalized title)\n"
    "- company_name: string|null\n"
    "- company_website: string|null (URL from post text only, do NOT invent)\n"
    "- company_linkedin: string|null (LinkedIn URL from post text only, do NOT invent)\n"
    "- recruiter: string|null\n"
    "- contacts: string[] (@username, emails, personal links, application form URLs)\n"
    "- domains: string[] from this set (lowercase): ['web3','ai','igaming','tech','gaming','traffic']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null\n"
    "- seniority: string|null\n"
    "- location_type: 'remote'|'hybrid'|'office'|null\n"
    "- salary_min_usd: integer|null\n"
    "- salary_max_usd: integer|null\n"
    "- stack: string[] (skills)\n"
    "- summary_en: string|null (<=300 chars, English)\n"
    "- description: string|null — ABOUT THE COMPANY/TEAM/PRODUCT. Extract the intro paragraph(s) that describe\n"
    "  who the company is, what they build, their product/project. This is NOT duties or requirements.\n"
    "  If the post starts with 'We are...', 'Our company...', 'We develop...' — that goes here.\n"
    "  English, Markdown. Return null only if no such info exists in the post.\n"
    "- responsibilities: string|null — WHAT THE CANDIDATE WILL DO (duties, tasks). English, Markdown with bullets.\n"
    "- requirements: string|null — WHAT IS REQUIRED from the candidate (skills, experience). English, Markdown with bullets.\n"
    "- conditions: string|null — WHAT THE COMPANY OFFERS (benefits, perks, salary, schedule). English, Markdown with bullets.\n"
    "- language: 'ru'|'en'|'other'|null\n"
    "- summary_ru: string|null (optional; prefer null)\n"
    "- metadata: object\n"
  )

  system = runtime.get("prompts", {}).get("vacancy_analyzer_system") or default_system

  url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
  }
  payload = {
    "model": runtime["openrouter"]["model_analyzer"],
    "messages": [
      {"role": "system", "content": system},
      {
        "role": "user",
        "content": text[: int(runtime.get("limits", {}).get("analyzer_max_chars", 12000))],
      },
    ],
    "temperature": 0.2,
    # Keep token budget reasonable to avoid 402 errors on low balances.
    "max_tokens": int(runtime.get("openrouter", {}).get("max_tokens_analyzer", 2500)),
  }

  async with httpx.AsyncClient(timeout=60) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]

  try:
    import json

    data = json.loads(content)
    if not isinstance(data, dict):
      raise ValueError("analyzer_returned_non_dict")
    # normalize domains lowercase + dedupe
    if "domains" in data and isinstance(data.get("domains"), list):
      data["domains"] = sorted({str(x).strip().lower() for x in data["domains"] if str(x).strip()})
    # trim summary_en
    if isinstance(data.get("summary_en"), str) and len(data["summary_en"]) > 300:
      data["summary_en"] = data["summary_en"][:297].rstrip() + "..."
    return data
  except Exception:
    return {
      "domains": [],
      "risk_label": None,
      "company_name": None,
      "title": None,
      "standardized_title": None,
      "role": None,
      "seniority": None,
      "location_type": None,
      "salary_min_usd": None,
      "salary_max_usd": None,
      "stack": [],
      "summary_ru": None,
      "summary_en": None,
      "recruiter": None,
      "contacts": [],
      "description": None,
      "responsibilities": None,
      "requirements": None,
      "conditions": None,
      "language": None,
      "metadata": {"raw": content},
    }


async def score_vacancy_with_openrouter(text: str, analysis: dict | None = None) -> dict:
  """
  Separate LLM step: score vacancy quality + 3 evidence-backed points (sentiment).

  Returns:
  {
    "score": int 0..100,
    "points": [{"sentiment": "positive|neutral|negative", "text": "...", "evidence": "..."}]  // length=3
  }
  """
  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return {
      "score": 50,
      "points": [
        {"sentiment": "neutral", "text": "Score is unavailable (OPENROUTER_API_KEY not set).", "evidence": "Not available"},
        {"sentiment": "neutral", "text": "Vacancy extraction still runs as a best-effort stub.", "evidence": "Stub mode"},
        {"sentiment": "neutral", "text": "Provide OPENROUTER_API_KEY to enable accurate scoring.", "evidence": "Missing API key"},
      ],
    }

  default_system = (
    "You are a strict JSON generator.\n"
    "You will score a Telegram job vacancy for usefulness/clarity for applicants.\n"
    "\n"
    "Rules:\n"
    "- Use ONLY information supported by the provided post text and extracted fields.\n"
    "- Do not invent salary, requirements, or contacts.\n"
    "- Points must be evidence-backed. If something is missing, mention the absence as evidence.\n"
    "- ALL output MUST be in ENGLISH ONLY (including evidence). Do NOT output any Russian/Cyrillic.\n"
    "- Do NOT copy long bullet lists or verbatim sentences from the post/extraction.\n"
    "- `text` is an evaluation sentence for applicants (quality/usefulness), NOT a quote.\n"
    "- `evidence` must be a short English phrase describing what was present/missing (max ~12 words), not a quote.\n"
    "- Output ONLY valid JSON (no markdown).\n"
    "\n"
    "Return JSON with keys:\n"
    "- score: integer from 0 to 100\n"
    "- points: array of exactly 3 objects\n"
    "  Each point object must have:\n"
    "  - sentiment: 'positive' | 'neutral' | 'negative'\n"
    "  - text: short English sentence (1 line)\n"
    "  - evidence: a short English phrase describing evidence (no quotes)\n"
    "\n"
    "Scoring guidance (not strict math):\n"
    "- Higher score if: clear role/title, concrete responsibilities/requirements, salary present, direct contacts.\n"
    "- Lower score if: responsibilities are vague/broad, requirements are missing, salary not provided, contacts absent.\n"
  )

  system = runtime.get("prompts", {}).get("vacancy_scorer_system") or default_system
  url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

  # Provide both raw text and the extraction to reduce hallucinations.
  extracted = analysis or {}

  def _has_value(v) -> bool:
    if v is None:
      return False
    if isinstance(v, str):
      return bool(v.strip())
    if isinstance(v, (list, tuple, set, dict)):
      return len(v) > 0
    return True

  def _heuristic_points_and_score(extracted_fields: dict) -> dict:
    # Stable 3-point output derived from extraction + obvious text signals.
    # We intentionally penalize unfavorable/unclear constraints to avoid overrating weak posts.
    has_role_title = (
      _has_value(extracted_fields.get("role"))
      or _has_value(extracted_fields.get("title"))
      or _has_value(extracted_fields.get("standardized_title"))
    )
    has_details = (
      _has_value(extracted_fields.get("responsibilities"))
      or _has_value(extracted_fields.get("requirements"))
      or _has_value(extracted_fields.get("description"))
    )
    has_contacts = _has_value(extracted_fields.get("contacts"))
    has_salary = _has_value(extracted_fields.get("salary_min_usd")) or _has_value(extracted_fields.get("salary_max_usd"))

    company_name = extracted_fields.get("company_name")
    has_company_info = _has_value(company_name)
    has_any_links = bool(re.search(r"https?://|www\.", text, flags=re.IGNORECASE))
    has_verified_website = bool(extracted_fields.get("_company_url_verified"))
    has_verified_linkedin = bool(extracted_fields.get("_company_linkedin_verified"))

    # Negative constraints from raw post text.
    is_night_shift = bool(
      re.search(r"\b7\s*/\s*0\b", text, flags=re.IGNORECASE)
      or re.search(r"\b17\s*:\s*00\b", text, flags=re.IGNORECASE)
      or re.search(r"\b05\s*:\s*00\b", text, flags=re.IGNORECASE)
      or re.search(r"night\s*shift|ночн", text, flags=re.IGNORECASE)
    )
    no_training = bool(
      re.search(r"no\s*training|we\s*do\s*not\s*train", text, flags=re.IGNORECASE)
      or re.search(r"не\s*обучаем|не\s*обуча|обучаем\s*не", text, flags=re.IGNORECASE)
    )

    # 1) Role + requirements quality
    if has_role_title and has_details:
      p1 = {
        "sentiment": "positive",
        "text": "A clear role/title and specific requirements are provided.",
        "evidence": "Role/title and requirements are explicitly mentioned.",
      }
    elif has_role_title:
      p1 = {
        "sentiment": "neutral",
        "text": "A role/title is provided, but requirements are limited.",
        "evidence": "Role/title is present; details are partially missing.",
      }
    else:
      p1 = {
        "sentiment": "negative",
        "text": "The vacancy does not specify a clear role/title.",
        "evidence": "Role/title is missing in the extraction.",
      }

    # 2) Contacts + salary completeness
    if has_contacts and has_salary:
      p2 = {
        "sentiment": "positive",
        "text": "Direct contacts and salary information are provided for applicants.",
        "evidence": "Contacts and salary are present in the extraction.",
      }
    elif has_contacts:
      p2 = {
        "sentiment": "neutral",
        "text": "Direct contacts are provided, but salary information is missing.",
        "evidence": "Contacts are present; salary is not mentioned.",
      }
    elif has_salary:
      p2 = {
        "sentiment": "neutral",
        "text": "Salary information is mentioned, but direct contacts are missing.",
        "evidence": "Salary is present; contacts are not mentioned.",
      }
    else:
      p2 = {
        "sentiment": "negative",
        "text": "Direct contacts and salary information are missing.",
        "evidence": "Contacts and salary are not mentioned.",
      }

    # 3) Red flags / transparency + company verification
    missing_company = (not has_company_info) or (not has_any_links)
    red_flags = []
    green_flags = []
    if is_night_shift:
      red_flags.append("night 7/0 shift is stated")
    if no_training:
      red_flags.append("no training is offered")
    if missing_company:
      red_flags.append("company info/links are missing")
    if has_verified_website:
      green_flags.append("company website verified")
    if has_verified_linkedin:
      green_flags.append("LinkedIn page verified")

    if red_flags and not green_flags:
      evidence = "; ".join(red_flags)[:140]
      p3 = {
        "sentiment": "negative",
        "text": "Work conditions and transparency reduce applicant fit.",
        "evidence": evidence,
      }
    elif red_flags and green_flags:
      evidence = ("Verified: " + ", ".join(green_flags) + "; Flags: " + ", ".join(red_flags))[:140]
      p3 = {
        "sentiment": "neutral",
        "text": "Company web presence is verified, but some work conditions raise concerns.",
        "evidence": evidence,
      }
    elif green_flags:
      evidence = ", ".join(green_flags)[:140]
      p3 = {
        "sentiment": "positive",
        "text": "The company has a verified web presence, increasing trust.",
        "evidence": evidence,
      }
    else:
      p3 = {
        "sentiment": "neutral",
        "text": "The vacancy provides useful details without major red flags.",
        "evidence": "No strong negative constraints detected in the post.",
      }

    # Score aligned with points.
    score = 40
    score += 18 if has_role_title else -10
    score += 20 if has_details else -12
    score += 10 if has_contacts else -12
    score += 10 if has_salary else -8
    if has_verified_website:
      score += 8
    if has_verified_linkedin:
      score += 5
    if is_night_shift:
      score -= 22
    if no_training:
      score -= 15
    if missing_company:
      score -= 15

    score = max(0, min(100, int(round(score))))
    return {"score": score, "points": [p1, p2, p3]}
  user_content = (
    "POST_TEXT:\n"
    + text[: int(runtime.get("limits", {}).get("analyzer_max_chars", 12000))]
    + "\n\nEXTRACTED_FIELDS (may contain nulls):\n"
    + __import__("json").dumps(extracted, ensure_ascii=False)
  )

  payload = {
    "model": runtime["openrouter"]["model_analyzer"],
    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
    "temperature": 0.0,
    "max_tokens": int(runtime.get("openrouter", {}).get("max_tokens_analyzer", 1500)),
  }

  async with httpx.AsyncClient(timeout=60) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]

  try:
    import json

    raw = (content or "").strip()

    # 1) Remove markdown code fences if any.
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    # 2) Try full parse.
    data = json.loads(raw)
    if not isinstance(data, dict):
      raise ValueError("scorer_returned_non_dict")
    points = data.get("points") or []
    # Normalize sentiment values.
    norm_points = []
    for p in points[:3]:
      if not isinstance(p, dict):
        continue
      sent = str(p.get("sentiment") or "").lower().strip()
      if sent not in {"positive", "neutral", "negative"}:
        sent = "neutral"
      # Normalize output to keep UI clean and avoid accidental markdown/bullets.
      out_text = str(p.get("text") or "").strip()
      out_evidence = str(p.get("evidence") or "").strip()
      # Collapse whitespace/newlines to single spaces.
      out_text = re.sub(r"\s+", " ", out_text).strip()
      out_evidence = re.sub(r"\s+", " ", out_evidence).strip()
      # Remove leading bullet markers like "•", "-", "1.", etc.
      out_text = re.sub(r"^[\-\u2022\u2013\u2014\*\d\.\)\s]+", "", out_text).strip()
      out_evidence = re.sub(r"^[\-\u2022\u2013\u2014\*\d\.\)\s]+", "", out_evidence).strip()
      # Enforce length (evidence should stay short).
      out_text = out_text[:160]
      out_evidence = out_evidence[:140]

      # If the model returned Cyrillic, we replace with a generic English evidence.
      # This is safer than showing Russian in the UI.
      if re.search(r"[\u0400-\u04FF\u0500-\u052F]", out_text):
        out_text = "The vacancy provides relevant details for applicants."
      if re.search(r"[\u0400-\u04FF\u0500-\u052F]", out_evidence):
        out_evidence = "Supported (or missing) per the post content."

      norm_points.append(
        {
          "sentiment": sent,
          "text": out_text or "No scoring data available.",
          "evidence": out_evidence or "Not mentioned in the text",
        }
      )

    # Model sometimes returns partially filled points with placeholders.
    # Use fuzzy matching to reliably detect them, then replace with deterministic heuristics.
    placeholder_detected = False
    if len(norm_points) < 3:
      placeholder_detected = True
    else:
      for pt in norm_points:
        text_v = str(pt.get("text") or "").strip().lower()
        ev_v = str(pt.get("evidence") or "").strip().lower()
        if "no scoring data available" in text_v:
          placeholder_detected = True
          break
        if "not mentioned" in ev_v:
          placeholder_detected = True
          break

    while len(norm_points) < 3:
      norm_points.append({"sentiment": "neutral", "text": "No scoring data available.", "evidence": "Not mentioned in the text"})
    score = data.get("score")
    try:
      score = int(score)
    except Exception:
      score = 50
    score = max(0, min(100, score))
    if placeholder_detected:
      heur = _heuristic_points_and_score(extracted)
      return {"score": heur["score"], "points": heur["points"][:3]}

    return {"score": score, "points": norm_points[:3]}
  except Exception:
    # Last resort: extract the first JSON object substring.
    try:
      import json, re

      raw2 = (content or "").strip()
      start = raw2.find("{")
      end = raw2.rfind("}")
      if start != -1 and end != -1 and end > start:
        data2 = json.loads(raw2[start : end + 1])
        if isinstance(data2, dict):
          points2 = data2.get("points") or []
          norm_points2 = []
          for p in points2[:3]:
            if not isinstance(p, dict):
              continue
            sent = str(p.get("sentiment") or "").lower().strip()
            if sent not in {"positive", "neutral", "negative"}:
              sent = "neutral"
          out_text = str(p.get("text") or "").strip()
          out_evidence = str(p.get("evidence") or "").strip()
          out_text = re.sub(r"\s+", " ", out_text).strip()
          out_evidence = re.sub(r"\s+", " ", out_evidence).strip()
          out_text = re.sub(r"^[\-\u2022\u2013\u2014\*\d\.\)\s]+", "", out_text).strip()
          out_evidence = re.sub(r"^[\-\u2022\u2013\u2014\*\d\.\)\s]+", "", out_evidence).strip()
          out_text = out_text[:160]
          out_evidence = out_evidence[:140]
          if re.search(r"[\u0400-\u04FF\u0500-\u052F]", out_text):
            out_text = "The vacancy provides relevant details for applicants."
          if re.search(r"[\u0400-\u04FF\u0500-\u052F]", out_evidence):
            out_evidence = "Supported (or missing) per the post content."

          norm_points2.append({"sentiment": sent, "text": out_text or "No scoring data available.", "evidence": out_evidence or "Not mentioned in the text"})
          while len(norm_points2) < 3:
            norm_points2.append(
              {"sentiment": "neutral", "text": "No scoring data available.", "evidence": "Not mentioned in the text"}
            )
          score2 = data2.get("score")
          try:
            score2_int = int(score2)
          except Exception:
            score2_int = 50
          score2_int = max(0, min(100, score2_int))
          placeholder2 = (
            len(norm_points2) < 3
            or any(
              (pt.get("text") == "No scoring data available." or "Not mentioned in the text" in (pt.get("evidence") or ""))
              for pt in norm_points2
            )
          )
          if placeholder2:
            heur = _heuristic_points_and_score(extracted)
            return {"score": heur["score"], "points": heur["points"][:3]}
          return {"score": score2_int, "points": norm_points2[:3]}
    except Exception:
      pass
    return {
      "score": 50,
      "points": [
        {"sentiment": "neutral", "text": "Scoring failed, defaulted to neutral.", "evidence": "Model output not parseable as JSON"},
        {"sentiment": "neutral", "text": "Use the re-analyze button to try again.", "evidence": "JSON parse failure"},
        {"sentiment": "neutral", "text": "Scoring is based on extraction fields and post text.", "evidence": "Extraction driven"},
      ],
    }


async def categorize_channel(title: str | None, bio: str | None, last_posts: list[str]) -> dict:
  """
  Analyze a channel by title + bio + last 3 posts.
  Returns:
    - ai_domains: string[] (web3/ai/igaming/tech/gaming/traffic)
    - ai_tags: string[] (free-form)
    - ai_risk_label: 'high-risk'|null
    - admin_contacts: string[] (from bio if present)
  """
  text = "\n\n".join(
    [
      f"TITLE:\n{title or ''}",
      f"BIO:\n{bio or ''}",
      "LAST_POSTS:\n" + "\n---\n".join(last_posts[:3]),
    ]
  ).strip()

  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  if not api_key:
    return {"ai_domains": [], "ai_tags": [], "ai_risk_label": None, "admin_contacts": []}

  url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
  system = """You are a strict JSON generator.
Goal: infer the CHANNEL'S THEME/ESSENCE, not summarize individual posts.
Use last posts only as weak signals/examples. Title+bio are primary.

Return ONLY valid JSON with keys:
  ai_domains: string[] from ['web3','ai','igaming','tech','gaming','traffic']
  ai_tags: string[] (EXACTLY 2 or 3 items, short, high-signal, no duplicates)
  ai_risk_label: 'high-risk'|null (only if scam/high-risk)
  admin_contacts: string[] (emails, @usernames, links) found in BIO only

Rules for ai_tags:
- max 3, min 2
- no generic noise like: 'jobs', 'vacancies', 'telegram', 'remote', 'hiring'
- prefer: role focus (e.g. 'backend', 'product'), geo focus ('EU', 'RU', 'Global'), or niche specifics ('defi', 'mlops')
"""
  system = runtime.get("prompts", {}).get("channel_categorizer_system") or system
  payload = {
    "model": runtime["openrouter"]["model_classifier"],
    "messages": [
      {"role": "system", "content": system},
      {"role": "user", "content": text[: int(runtime.get("limits", {}).get("channel_max_chars", 7000))]},
    ],
    "temperature": 0.0,
  }
  async with httpx.AsyncClient(timeout=40) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
  try:
    import json

    data = json.loads(content)
    return {
      "ai_domains": [str(x).lower() for x in (data.get("ai_domains") or []) if str(x).strip()],
      "ai_tags": [str(x) for x in (data.get("ai_tags") or []) if str(x).strip()],
      "ai_risk_label": data.get("ai_risk_label"),
      "admin_contacts": [str(x) for x in (data.get("admin_contacts") or []) if str(x).strip()],
    }
  except Exception:
    return {"ai_domains": [], "ai_tags": [], "ai_risk_label": None, "admin_contacts": [], "raw": content}

  url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
  headers = {
    "Authorization": f"Bearer {settings.openrouter_api_key}",
    "Content-Type": "application/json",
  }
  system = (
    "You are a strict JSON generator. Analyze a Telegram job vacancy post and extract structured data.\n"
    "Return ONLY valid JSON (no markdown, no code blocks).\n"
    "\n"
    "IMPORTANT:\n"
    "- A vacancy can belong to MULTIPLE domains (e.g. ['ai','web3']).\n"
    "- ALL textual output fields MUST be in ENGLISH (even if input is RU). Do NOT output Russian.\n"
    "- summary_en MUST be short and useful for a listing card (max 300 chars).\n"
    "- description/requirements/conditions/responsibilities must be English bullet-like text (can be plain paragraphs).\n"
    "- contacts MUST include ALL ways to apply/respond: @username handles, emails, personal links,\n"
    "  AND application form URLs (Google Forms, Typeform, JotForm, etc.).\n"
    "  EXCLUDE only: channel footer links, \"subscribe\" links, and generic channel usernames.\n"
    "- company_website: if the post contains a URL to the company's own website or career page, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "- company_linkedin: if the post contains a LinkedIn company/profile URL, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "\n"
    "Required keys:\n"
    "- title: string|null\n"
    "- standardized_title: string|null (normalized title)\n"
    "- company_name: string|null\n"
    "- company_website: string|null (URL from post text only, do NOT invent)\n"
    "- company_linkedin: string|null (LinkedIn URL from post text only, do NOT invent)\n"
    "- recruiter: string|null\n"
    "- contacts: string[] (@username, emails, personal links, application form URLs)\n"
    "- domains: string[] from this set (lowercase): ['web3','ai','igaming','tech','gaming','traffic']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null\n"
    "- seniority: string|null\n"
    "- location_type: 'remote'|'hybrid'|'office'|null\n"
    "- salary_min_usd: integer|null\n"
    "- salary_max_usd: integer|null\n"
    "- stack: string[] (skills)\n"
    "- summary_en: string|null (<=300 chars, English)\n"
    "- description: string|null — ABOUT THE COMPANY/TEAM/PRODUCT (intro paragraphs). English.\n"
    "- responsibilities: string|null — duties/tasks. English.\n"
    "- requirements: string|null — skills/experience required. English.\n"
    "- conditions: string|null — benefits/perks/offers. English.\n"
    "- language: 'ru'|'en'|'other'|null\n"
    "- ai_score_value: integer 0-10\n"
    "- summary_ru: string|null (optional; prefer null)\n"
    "- metadata: object\n"
  )
  payload = {
    "model": settings.openrouter_model_analyzer,
    "messages": [
      {"role": "system", "content": system},
      {"role": "user", "content": text},
    ],
    "temperature": 0.2,
  }
  async with httpx.AsyncClient(timeout=60) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
  try:
    import json

    data = json.loads(content)
    if not isinstance(data, dict):
      raise ValueError("analyzer_returned_non_dict")
    # ensure domains lowercase + dedupe
    if "domains" in data and isinstance(data.get("domains"), list):
      data["domains"] = sorted({str(x).strip().lower() for x in data["domains"] if str(x).strip()})
    # trim summary_en
    if isinstance(data.get("summary_en"), str) and len(data["summary_en"]) > 300:
      data["summary_en"] = data["summary_en"][:297].rstrip() + "..."
    return data
  except Exception:
    return {
      "domains": [],
      "risk_label": None,
      "ai_score_value": 5,
      "company_name": None,
      "title": None,
      "standardized_title": None,
      "role": None,
      "seniority": None,
      "location_type": None,
      "salary_min_usd": None,
      "salary_max_usd": None,
      "stack": [],
      "summary_ru": None,
      "summary_en": None,
      "recruiter": None,
      "contacts": [],
      "description": None,
      "responsibilities": None,
      "requirements": None,
      "conditions": None,
      "language": None,
      "metadata": {"raw": content},
    }
