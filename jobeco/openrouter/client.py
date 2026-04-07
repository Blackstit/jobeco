from __future__ import annotations

import json as _json
import os
import re
from urllib.parse import urlparse

import httpx
import structlog

from jobeco.settings import settings
from jobeco.runtime_settings import get_runtime_settings
from jobeco.openrouter._enrich_company import enrich_company_profile  # noqa: F401
from jobeco.processing.company_branding import is_ats_or_job_board_url

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
  if is_ats_or_job_board_url(url):
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
    "- domains: string[] — one or more from this set (lowercase, a vacancy can belong to MULTIPLE):\n"
    "  ['web3','crypto','defi','nft','dao','gamefi','rwa','l1l2','ai','igaming','tech','gaming','traffic','design','dev','fintech','marketing','hr','analytics','product','support']\n"
    "  IMPORTANT: 'web3' and 'crypto' are broad umbrella categories. Sub-verticals like 'defi','nft','dao','gamefi','rwa','l1l2' MUST be added IN ADDITION to 'web3'/'crypto', not instead of them.\n"
    "  Pick ALL that apply. Examples:\n"
    "  - A Solidity dev for a DEX → ['dev','web3','crypto','defi']\n"
    "  - A frontend dev for an NFT marketplace → ['dev','web3','nft']\n"
    "  - A community manager for a DAO → ['web3','dao','marketing']\n"
    "  - A game designer for play-to-earn → ['gaming','gamefi','web3','crypto']\n"
    "  - A protocol engineer for L2 scaling → ['dev','web3','crypto','l1l2']\n"
    "  - A compliance officer for RWA tokenization → ['web3','crypto','rwa','fintech']\n"
    "  - An AI engineer → ['ai','dev']\n"
    "  - A graphic designer for a traffic team → ['design','traffic']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null — pick EXACTLY ONE from this canonical list (Title Case):\n"
    "  'Backend Developer'|'Frontend Developer'|'Full Stack Developer'|'Mobile Developer'|\n"
    "  'Blockchain Developer'|'Smart Contract Developer'|'DevOps Engineer'|'QA Engineer'|\n"
    "  'Security Engineer'|'System Administrator'|'Data Analyst'|'Data Engineer'|\n"
    "  'Data Scientist'|'ML Engineer'|'Product Manager'|'Project Manager'|'Product Owner'|\n"
    "  'Business Analyst'|'System Analyst'|'UI/UX Designer'|'Graphic Designer'|\n"
    "  'Motion Designer'|'3D Artist'|'Game Designer'|'Marketing Manager'|'Media Buyer'|\n"
    "  'SEO Specialist'|'SMM Manager'|'Content Manager'|'Community Manager'|\n"
    "  'Traffic Manager'|'Affiliate Manager'|'Growth Manager'|'Performance Marketing Manager'|\n"
    "  'CRM Manager'|'PR Manager'|'Sales Manager'|'Business Development Manager'|\n"
    "  'Account Manager'|'Partnerships Manager'|'Financial Manager'|'Risk Analyst'|\n"
    "  'Compliance Manager'|'Legal Counsel'|'HR Manager'|'Recruiter'|'Customer Support'|\n"
    "  'Operations Manager'|'Executive'|'Other'\n"
    "  Map specific titles to the closest canonical role. E.g. 'PHP Developer' → 'Backend Developer',\n"
    "  'UX/UI Designer' → 'UI/UX Designer', 'Growth Lead' → 'Growth Manager'.\n"
    "  Do NOT include seniority in the role (e.g. 'Senior Backend Developer' → role='Backend Developer').\n"
    "- seniority: string|null — one of: 'trainee'|'junior'|'middle'|'senior'|'lead'|'head'|'c-level'|null\n"
    "  Always lowercase. If text says 'Junior/Middle' pick the higher one ('middle').\n"
    "  'Team Lead' → 'lead'. 'Head of...' → 'head'. 'CTO/CPO/CEO' → 'c-level'.\n"
    "- employment_type: string|null — one of: 'full-time'|'part-time'|'project'|'freelance'|'internship'|null\n"
    "- language_requirements: object|null — language proficiency required, e.g. {\"english\": \"B2\", \"russian\": \"C1\"}.\n"
    "  Keys are lowercase language names, values are level codes (A1/A2/B1/B2/C1/C2/native) or 'any'.\n"
    "  Extract ONLY if explicitly mentioned in the text. null if no language info.\n"
    "- english_level: string|null — shortcut: the English level from language_requirements if present (e.g. 'B2').\n"
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


_SCORING_CRITERIA = [
  {"key": "tasks_and_kpi",       "label": "Tasks & KPI clarity",   "weight": 0.30},
  {"key": "compensation_clarity", "label": "Compensation clarity", "weight": 0.25},
  {"key": "tech_stack_and_ops",  "label": "Stack & processes",     "weight": 0.20},
  {"key": "requirement_logic",   "label": "Requirement logic",     "weight": 0.15},
  {"key": "company_profile",     "label": "Company profile",       "weight": 0.10},
]


async def score_vacancy_with_openrouter(text: str, analysis: dict | None = None) -> dict:
  """
  Score vacancy quality using 5 weighted criteria.

  Returns dict compatible with the new format:
  {
    "total_score": float 0..10,
    "overall_summary": str,
    "red_flags": [str, ...],
    "scoring_results": [
      {"criterion": str, "key": str, "score": int 0..10, "weight": float, "summary": str},
      ...  # exactly 5
    ]
  }
  """
  runtime = await get_runtime_settings()
  api_key = runtime.get("openrouter", {}).get("api_key") or ""
  extracted = analysis or {}

  if not api_key:
    return _heuristic_scoring(text, extracted)

  default_system = (
    "You are a strict JSON generator.\n"
    "Analyze the provided Telegram job vacancy and score its quality for applicants.\n"
    "\n"
    "Score the vacancy on 5 criteria (each 0-10):\n"
    "1. tasks_and_kpi (weight 0.30) — How specific are the responsibilities? Are deliverables/KPIs measurable?\n"
    "2. compensation_clarity (weight 0.25) — Is salary range stated with currency? Are payment terms clear?\n"
    "3. tech_stack_and_ops (weight 0.20) — Are tools, technologies, and work processes described?\n"
    "4. requirement_logic (weight 0.15) — Do required skills/experience match the stated seniority?\n"
    "5. company_profile (weight 0.10) — Is the company/product understandable? Are there links/socials?\n"
    "\n"
    "Rules:\n"
    "- ALL output MUST be in ENGLISH ONLY. No Russian/Cyrillic.\n"
    "- Use ONLY information from the provided text and extracted fields.\n"
    "- `summary` is a concise evaluation sentence (max ~25 words), NOT a quote from the post.\n"
    "- `overall_summary` is 1 sentence (max ~30 words) summarizing overall quality.\n"
    "- `red_flags` is an array of short strings for red flags (empty array if none). Examples:\n"
    "  night shift, no training, suspicious scheme, MLM, no company info, unrealistic salary.\n"
    "- Output ONLY valid JSON (no markdown fences).\n"
    "\n"
    "Return JSON:\n"
    "{\n"
    '  "scoring_results": [\n'
    '    {"criterion": "Tasks & KPI clarity", "key": "tasks_and_kpi", "score": <0-10>, "summary": "..."},\n'
    '    {"criterion": "Compensation clarity", "key": "compensation_clarity", "score": <0-10>, "summary": "..."},\n'
    '    {"criterion": "Stack & processes", "key": "tech_stack_and_ops", "score": <0-10>, "summary": "..."},\n'
    '    {"criterion": "Requirement logic", "key": "requirement_logic", "score": <0-10>, "summary": "..."},\n'
    '    {"criterion": "Company profile", "key": "company_profile", "score": <0-10>, "summary": "..."}\n'
    "  ],\n"
    '  "overall_summary": "...",\n'
    '  "red_flags": []\n'
    "}\n"
  )

  system = runtime.get("prompts", {}).get("vacancy_scorer_system") or default_system
  url = runtime["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
  headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

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

  return _parse_scoring_response(content, text, extracted)


def _clean_text(s: str, max_len: int = 200) -> str:
  s = re.sub(r"\s+", " ", (s or "")).strip()
  s = re.sub(r"^[\-\u2022\u2013\u2014\*\d\.\)\s]+", "", s).strip()
  if re.search(r"[\u0400-\u04FF\u0500-\u052F]", s):
    s = ""
  return s[:max_len]


def _parse_scoring_response(content: str, text: str, extracted: dict) -> dict:
  import json as _json

  raw = (content or "").strip()
  raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
  raw = re.sub(r"\s*```$", "", raw)

  data = None
  try:
    data = _json.loads(raw)
  except Exception:
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
      try:
        data = _json.loads(raw[start:end + 1])
      except Exception:
        pass

  if not isinstance(data, dict):
    return _heuristic_scoring(text, extracted)

  raw_results = data.get("scoring_results") or []
  if not isinstance(raw_results, list) or len(raw_results) < 3:
    return _heuristic_scoring(text, extracted)

  criteria_keys = [c["key"] for c in _SCORING_CRITERIA]
  criteria_map = {c["key"]: c for c in _SCORING_CRITERIA}
  results = []
  for item in raw_results:
    if not isinstance(item, dict):
      continue
    key = str(item.get("key") or "").strip()
    if key not in criteria_map:
      continue
    sc = item.get("score")
    try:
      sc = max(0, min(10, int(sc)))
    except Exception:
      sc = 5
    summary = _clean_text(str(item.get("summary") or ""))
    if not summary:
      summary = "No details provided."
    c = criteria_map[key]
    results.append({
      "criterion": c["label"],
      "key": key,
      "score": sc,
      "weight": c["weight"],
      "summary": summary,
    })

  seen_keys = {r["key"] for r in results}
  for c in _SCORING_CRITERIA:
    if c["key"] not in seen_keys:
      results.append({
        "criterion": c["label"],
        "key": c["key"],
        "score": 5,
        "weight": c["weight"],
        "summary": "Not enough information to evaluate.",
      })

  results.sort(key=lambda r: criteria_keys.index(r["key"]))

  total = sum(r["score"] * r["weight"] for r in results)
  total = round(max(0.0, min(10.0, total)), 1)

  overall = _clean_text(str(data.get("overall_summary") or ""))
  if not overall:
    overall = "Vacancy quality assessment based on 5 criteria."

  red_flags = []
  raw_flags = data.get("red_flags") or []
  if isinstance(raw_flags, list):
    for f in raw_flags[:10]:
      ft = _clean_text(str(f), 100)
      if ft:
        red_flags.append(ft)

  return {
    "total_score": total,
    "overall_summary": overall,
    "red_flags": red_flags,
    "scoring_results": results,
  }


def _has_value(v) -> bool:
  if v is None:
    return False
  if isinstance(v, str):
    return bool(v.strip())
  if isinstance(v, (list, tuple, set, dict)):
    return len(v) > 0
  return True


def _heuristic_scoring(text: str, extracted: dict) -> dict:
  """Deterministic fallback scoring using extracted fields."""
  has_role = _has_value(extracted.get("role")) or _has_value(extracted.get("title"))
  has_resp = _has_value(extracted.get("responsibilities"))
  has_req = _has_value(extracted.get("requirements"))
  has_desc = _has_value(extracted.get("description"))
  has_contacts = _has_value(extracted.get("contacts"))
  has_salary = _has_value(extracted.get("salary_min_usd")) or _has_value(extracted.get("salary_max_usd"))
  has_stack = _has_value(extracted.get("stack"))
  has_company = _has_value(extracted.get("company_name"))
  has_links = bool(re.search(r"https?://|www\.", text, flags=re.IGNORECASE))
  has_verified_web = bool(extracted.get("_company_url_verified"))
  has_verified_li = bool(extracted.get("_company_linkedin_verified"))

  is_night = bool(re.search(r"\b7\s*/\s*0\b|night\s*shift|ночн", text, re.I))
  no_train = bool(re.search(r"не\s*обучаем|no\s*training|we\s*do\s*not\s*train", text, re.I))

  # tasks_and_kpi
  t1 = 3
  if has_role: t1 += 2
  if has_resp: t1 += 3
  if has_req: t1 += 2
  t1 = min(10, t1)
  s1 = "Clear responsibilities and role are described." if t1 >= 7 else ("Some task info present but lacking specifics." if t1 >= 4 else "Tasks and KPIs are not clearly defined.")

  # compensation_clarity
  t2 = 2
  if has_salary: t2 += 6
  if has_contacts: t2 += 2
  t2 = min(10, t2)
  s2 = "Salary range and contact details are provided." if t2 >= 8 else ("Partial compensation info available." if t2 >= 4 else "No salary or payment details mentioned.")

  # tech_stack_and_ops
  t3 = 3
  if has_stack: t3 += 4
  if has_resp: t3 += 2
  if has_desc: t3 += 1
  t3 = min(10, t3)
  s3 = "Tech stack and processes are well documented." if t3 >= 7 else ("Some technical details mentioned." if t3 >= 4 else "No stack or process information provided.")

  # requirement_logic
  t4 = 4
  if has_req: t4 += 3
  if has_role: t4 += 2
  if _has_value(extracted.get("seniority")): t4 += 1
  t4 = min(10, t4)
  s4 = "Requirements match the stated role level." if t4 >= 7 else ("Requirements are partially defined." if t4 >= 4 else "Requirements are vague or missing.")

  # company_profile
  t5 = 2
  if has_company: t5 += 2
  if has_links: t5 += 1
  if has_desc: t5 += 2
  if has_verified_web: t5 += 2
  if has_verified_li: t5 += 1
  t5 = min(10, t5)
  s5 = "Company identity is clear with verified presence." if t5 >= 7 else ("Some company info available." if t5 >= 4 else "Company profile is unclear or missing.")

  red_flags = []
  if is_night:
    red_flags.append("Night/extreme work schedule detected")
    t1 = max(0, t1 - 2)
    t2 = max(0, t2 - 1)
  if no_train:
    red_flags.append("No training offered")
    t4 = max(0, t4 - 2)
  if not has_company and not has_links:
    red_flags.append("No company info or links")

  results = [
    {"criterion": "Tasks & KPI clarity", "key": "tasks_and_kpi", "score": t1, "weight": 0.30, "summary": s1},
    {"criterion": "Compensation clarity", "key": "compensation_clarity", "score": t2, "weight": 0.25, "summary": s2},
    {"criterion": "Stack & processes", "key": "tech_stack_and_ops", "score": t3, "weight": 0.20, "summary": s3},
    {"criterion": "Requirement logic", "key": "requirement_logic", "score": t4, "weight": 0.15, "summary": s4},
    {"criterion": "Company profile", "key": "company_profile", "score": t5, "weight": 0.10, "summary": s5},
  ]

  total = round(sum(r["score"] * r["weight"] for r in results), 1)
  total = max(0.0, min(10.0, total))

  overall = "Vacancy quality estimated from extracted structured fields."

  return {
    "total_score": total,
    "overall_summary": overall,
    "red_flags": red_flags,
    "scoring_results": results,
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
  ai_domains: string[] from ['web3','crypto','defi','nft','dao','gamefi','rwa','l1l2','ai','igaming','tech','gaming','traffic','design','dev','fintech','marketing','hr','analytics','product','support']
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
    "- domains: string[] — one or more from: ['web3','ai','igaming','tech','gaming','traffic','design','dev','fintech','crypto','marketing','hr','analytics','product','support']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null — pick EXACTLY ONE from canonical list:\n"
    "  'Backend Developer'|'Frontend Developer'|'Full Stack Developer'|'Mobile Developer'|\n"
    "  'Blockchain Developer'|'Smart Contract Developer'|'DevOps Engineer'|'QA Engineer'|\n"
    "  'Security Engineer'|'System Administrator'|'Data Analyst'|'Data Engineer'|\n"
    "  'Data Scientist'|'ML Engineer'|'Product Manager'|'Project Manager'|'Product Owner'|\n"
    "  'Business Analyst'|'System Analyst'|'UI/UX Designer'|'Graphic Designer'|\n"
    "  'Motion Designer'|'3D Artist'|'Game Designer'|'Marketing Manager'|'Media Buyer'|\n"
    "  'SEO Specialist'|'SMM Manager'|'Content Manager'|'Community Manager'|\n"
    "  'Traffic Manager'|'Affiliate Manager'|'Growth Manager'|'Performance Marketing Manager'|\n"
    "  'CRM Manager'|'PR Manager'|'Sales Manager'|'Business Development Manager'|\n"
    "  'Account Manager'|'Partnerships Manager'|'Financial Manager'|'Risk Analyst'|\n"
    "  'Compliance Manager'|'Legal Counsel'|'HR Manager'|'Recruiter'|'Customer Support'|\n"
    "  'Operations Manager'|'Executive'|'Other'\n"
    "  Do NOT include seniority prefix in the role.\n"
    "- seniority: string|null — one of: 'trainee'|'junior'|'middle'|'senior'|'lead'|'head'|'c-level'|null\n"
    "  Always lowercase. 'Team Lead' → 'lead'. 'Head of...' → 'head'. CTO/CPO → 'c-level'.\n"
    "- employment_type: string|null — one of: 'full-time'|'part-time'|'project'|'freelance'|'internship'|null\n"
    "- language_requirements: object|null — e.g. {\"english\": \"B2\", \"russian\": \"C1\"}.\n"
    "  Keys are lowercase language names, values are level codes (A1-C2/native). null if not mentioned.\n"
    "- english_level: string|null — the English level if present (e.g. 'B2').\n"
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
