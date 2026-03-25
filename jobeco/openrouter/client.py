from __future__ import annotations

import os

import httpx

from jobeco.settings import settings
from jobeco.runtime_settings import get_runtime_settings


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
    "- contacts MUST be direct hiring contacts (HR/recruiter/manager). EXCLUDE channel footer links, \"subscribe\" links,\n"
    "  and generic channel usernames. If in doubt, omit.\n"
    "\n"
    "Required keys:\n"
    "- title: string|null\n"
    "- standardized_title: string|null (normalized title)\n"
    "- company_name: string|null\n"
    "- recruiter: string|null\n"
    "- contacts: string[] (ONLY direct: @username, emails, personal links)\n"
    "- domains: string[] from this set (lowercase): ['web3','ai','igaming','tech','gaming','traffic']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null\n"
    "- seniority: string|null\n"
    "- location_type: 'remote'|'hybrid'|'office'|null\n"
    "- salary_min_usd: integer|null\n"
    "- salary_max_usd: integer|null\n"
    "- stack: string[] (skills)\n"
    "- summary_en: string|null (<=300 chars, English)\n"
    "- description: string|null (English, Markdown with bullets)\n"
    "- responsibilities: string|null (English, Markdown with bullets)\n"
    "- requirements: string|null (English, Markdown with bullets)\n"
    "- conditions: string|null (English, Markdown with bullets)\n"
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
    "- Output ONLY valid JSON (no markdown).\n"
    "\n"
    "Return JSON with keys:\n"
    "- score: integer from 0 to 100\n"
    "- points: array of exactly 3 objects\n"
    "  Each point object must have:\n"
    "  - sentiment: 'positive' | 'neutral' | 'negative'\n"
    "  - text: short English sentence (1 line)\n"
    "  - evidence: a short snippet or 'Not mentioned in the text'\n"
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
  user_content = (
    "POST_TEXT:\n"
    + text[: int(runtime.get("limits", {}).get("analyzer_max_chars", 12000))]
    + "\n\nEXTRACTED_FIELDS (may contain nulls):\n"
    + str(extracted)
  )

  payload = {
    "model": runtime["openrouter"]["model_analyzer"],
    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
    "temperature": 0.2,
    "max_tokens": int(runtime.get("openrouter", {}).get("max_tokens_analyzer", 1500)),
  }

  async with httpx.AsyncClient(timeout=60) as client:
    r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]

  try:
    import json

    data = json.loads(content)
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
      norm_points.append(
        {
          "sentiment": sent,
          "text": str(p.get("text") or "").strip(),
          "evidence": str(p.get("evidence") or "").strip() or "Not mentioned in the text",
        }
      )
    while len(norm_points) < 3:
      norm_points.append(
        {"sentiment": "neutral", "text": "No scoring data available.", "evidence": "Not mentioned in the text"}
      )
    score = data.get("score")
    try:
      score = int(score)
    except Exception:
      score = 50
    score = max(0, min(100, score))
    return {"score": score, "points": norm_points[:3]}
  except Exception:
    return {
      "score": 50,
      "points": [
        {"sentiment": "neutral", "text": "Scoring failed, defaulted to neutral.", "evidence": "Model parse failed"},
        {"sentiment": "neutral", "text": "Provide stable OpenRouter responses to enable scoring.", "evidence": "Parse failure"},
        {"sentiment": "neutral", "text": "Use extraction fields to verify presence/absence.", "evidence": "Extraction driven"},
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
    "- contacts MUST be direct hiring contacts (HR/recruiter/manager). EXCLUDE channel footer links, \"subscribe\" links,\n"
    "  and generic channel usernames. If in doubt, omit.\n"
    "\n"
    "Required keys:\n"
    "- title: string|null\n"
    "- standardized_title: string|null (normalized title)\n"
    "- company_name: string|null\n"
    "- recruiter: string|null\n"
    "- contacts: string[] (ONLY direct: @username, emails, personal links)\n"
    "- domains: string[] from this set (lowercase): ['web3','ai','igaming','tech','gaming','traffic']\n"
    "- risk_label: 'high-risk'|null (only if scam/high-risk)\n"
    "- role: string|null\n"
    "- seniority: string|null\n"
    "- location_type: 'remote'|'hybrid'|'office'|null\n"
    "- salary_min_usd: integer|null\n"
    "- salary_max_usd: integer|null\n"
    "- stack: string[] (skills)\n"
    "- summary_en: string|null (<=300 chars, English)\n"
    "- description: string|null (English)\n"
    "- responsibilities: string|null (English)\n"
    "- requirements: string|null (English)\n"
    "- conditions: string|null (English)\n"
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
