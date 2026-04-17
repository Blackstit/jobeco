"""
A/B test for analyzer + scorer across multiple OpenRouter models.

Pulls last N vacancies raw_text, runs each through candidate models
using the same system prompts as production (analyze_with_openrouter,
score_vacancy_with_openrouter). Captures cost (OpenRouter usage),
latency, extracted fields, and produces a comparison report.

Usage:
  docker compose exec admin-web python -m scripts.ab_models_test
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from jobeco.db.models import Vacancy
from jobeco.db.session import SessionLocal
from jobeco.openrouter.client import (
    _parse_scoring_response,
    _SCORING_CRITERIA,
)
from jobeco.runtime_settings import get_runtime_settings


# -- Models to compare (first one is baseline) --------------------------------
MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-v3.2",
    "google/gemini-2.5-flash",
    "qwen/qwen-2.5-72b-instruct",
]

VACANCY_LIMIT = 10
MIN_RAW_LEN = 200

OUT_DIR = Path("/tmp/ab_models")
OUT_DIR.mkdir(exist_ok=True, parents=True)


# -- Prompts (duplicated here to run under configurable model, identical to prod) --
ANALYZER_SYSTEM = (
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
    "  3) Translate faithfully - preserve ALL bullet points that exist in the source. Do NOT summarize or compress.\n"
    "  4) If the post has multiple roles/positions, combine their requirements under clear sub-headings (e.g. '### Role Name').\n"
    "  5) If a section is genuinely absent from the text, return null. But if bullets exist - include ALL of them.\n"
    "- contacts MUST include ALL ways to apply/respond: @username handles, emails, personal links,\n"
    "  AND application form URLs (Google Forms, Typeform, JotForm, etc.).\n"
    "  EXCLUDE: channel self-promotion links, 'subscribe' links, and @usernames that are clearly\n"
    "  the posting/aggregator channel.\n"
    "- company_website: if the post contains a URL to the company's own website or career page, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "- company_linkedin: if the post contains a LinkedIn company/profile URL, extract it.\n"
    "  Do NOT guess/invent URLs. Only extract if explicitly present in the text.\n"
    "\n"
    "Required keys:\n"
    "- title: string|null\n"
    "- standardized_title: string|null\n"
    "- company_name: string|null\n"
    "- company_website: string|null\n"
    "- company_linkedin: string|null\n"
    "- recruiter: string|null\n"
    "- contacts: string[]\n"
    "- domains: string[] from ['web3','crypto','defi','nft','dao','gamefi','rwa','l1l2','ai','igaming','tech','gaming','traffic','design','dev','fintech','marketing','hr','analytics','product','support']\n"
    "  'web3'/'crypto' are umbrellas - sub-verticals MUST be added IN ADDITION, not instead.\n"
    "- risk_label: 'high-risk'|null\n"
    "- role: string|null - pick EXACTLY ONE from canonical list (Title Case):\n"
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
    "  Do NOT include seniority in role.\n"
    "- seniority: 'trainee'|'junior'|'middle'|'senior'|'lead'|'head'|'c-level'|null (lowercase)\n"
    "- employment_type: 'full-time'|'part-time'|'project'|'freelance'|'internship'|null\n"
    "- language_requirements: object|null\n"
    "- english_level: string|null\n"
    "- location_type: 'remote'|'hybrid'|'office'|null\n"
    "- salary_min_usd: integer|null\n"
    "- salary_max_usd: integer|null\n"
    "- stack: string[]\n"
    "- summary_en: string|null (<=300 chars)\n"
    "- description: string|null (about company/team/product, Markdown)\n"
    "- responsibilities: string|null (what candidate will do, Markdown)\n"
    "- requirements: string|null (what is required, Markdown)\n"
    "- conditions: string|null (what company offers, Markdown)\n"
    "- language: 'ru'|'en'|'other'|null\n"
    "- summary_ru: string|null (optional; prefer null)\n"
    "- metadata: object\n"
)

SCORER_SYSTEM = (
    "You are a strict JSON generator.\n"
    "Analyze the provided Telegram job vacancy and score its quality for applicants.\n"
    "\n"
    "Score the vacancy on 5 criteria (each 0-10):\n"
    "1. tasks_and_kpi (weight 0.30) - How specific are the responsibilities? Are deliverables/KPIs measurable?\n"
    "2. compensation_clarity (weight 0.25) - Is salary range stated with currency? Are payment terms clear?\n"
    "3. tech_stack_and_ops (weight 0.20) - Are tools, technologies, and work processes described?\n"
    "4. requirement_logic (weight 0.15) - Do required skills/experience match the stated seniority?\n"
    "5. company_profile (weight 0.10) - Is the company/product understandable? Are there links/socials?\n"
    "\n"
    "Rules:\n"
    "- ALL output MUST be in ENGLISH ONLY. No Russian/Cyrillic.\n"
    "- Use ONLY information from the provided text and extracted fields.\n"
    "- `summary` is a concise evaluation sentence (max ~25 words), NOT a quote from the post.\n"
    "- `overall_summary` is 1 sentence (max ~30 words).\n"
    "- `red_flags` is an array of short strings (empty array if none).\n"
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


async def call_openrouter(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
) -> dict:
    """
    Returns {content, usage, cost_usd, latency_s, http_status, error}.
    Uses OpenRouter 'usage: include' extra param to get exact cost.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # OpenRouter returns granular cost when 'usage.include' is true
        "usage": {"include": True},
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(url, headers=headers, json=payload)
            dt = time.monotonic() - t0
            try:
                data = r.json()
            except Exception:
                data = {"_raw": r.text[:400]}
            if r.status_code >= 400:
                return {
                    "content": None, "usage": None, "cost_usd": None,
                    "latency_s": dt, "http_status": r.status_code,
                    "error": data.get("error") or data,
                }
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content")
            usage = data.get("usage") or {}
            # OpenRouter exposes either usage.cost or usage.total_cost
            cost = usage.get("cost")
            if cost is None:
                cost = usage.get("total_cost")
            return {
                "content": content, "usage": usage,
                "cost_usd": float(cost) if cost is not None else None,
                "latency_s": dt, "http_status": r.status_code,
                "error": None,
            }
    except Exception as exc:
        return {
            "content": None, "usage": None, "cost_usd": None,
            "latency_s": time.monotonic() - t0,
            "http_status": None,
            "error": f"exception: {type(exc).__name__}: {exc}",
        }


def parse_analyzer_json(content: str | None) -> dict:
    """Best-effort JSON parse of analyzer output (strips ```json fences)."""
    if not content:
        return {}
    raw = content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```\s*$", "", raw)
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e > s:
            try:
                data = json.loads(raw[s:e + 1])
            except Exception:
                return {"_parse_error": True, "_raw": raw[:800]}
        else:
            return {"_parse_error": True, "_raw": raw[:800]}
    if not isinstance(data, dict):
        return {"_parse_error": True, "_raw": raw[:800]}
    if isinstance(data.get("domains"), list):
        data["domains"] = sorted({str(x).strip().lower() for x in data["domains"] if str(x).strip()})
    return data


async def run_model_for_vacancy(
    *,
    api_key: str,
    base_url: str,
    model: str,
    raw_text: str,
) -> dict:
    """Run analyzer + scorer for one vacancy on one model."""
    analyzer_input = raw_text[:12000]

    an_res = await call_openrouter(
        api_key=api_key, base_url=base_url, model=model,
        system=ANALYZER_SYSTEM, user=analyzer_input,
        temperature=0.2, max_tokens=4000,
    )
    analysis = parse_analyzer_json(an_res["content"]) if an_res["content"] else {}

    # Scorer uses same input format as prod
    scorer_input = (
        "POST_TEXT:\n" + analyzer_input +
        "\n\nEXTRACTED_FIELDS (may contain nulls):\n" +
        json.dumps(analysis, ensure_ascii=False)
    )
    sc_res = await call_openrouter(
        api_key=api_key, base_url=base_url, model=model,
        system=SCORER_SYSTEM, user=scorer_input,
        temperature=0.0, max_tokens=1500,
    )
    scoring = _parse_scoring_response(sc_res["content"] or "", raw_text, analysis) if sc_res["content"] else None

    total_cost = None
    if an_res["cost_usd"] is not None or sc_res["cost_usd"] is not None:
        total_cost = (an_res["cost_usd"] or 0.0) + (sc_res["cost_usd"] or 0.0)

    return {
        "model": model,
        "analyzer": {
            "latency_s": round(an_res["latency_s"], 3),
            "cost_usd": an_res["cost_usd"],
            "usage": an_res["usage"],
            "http_status": an_res["http_status"],
            "error": an_res["error"],
            "parse_error": bool(analysis.get("_parse_error")),
            "analysis": analysis,
        },
        "scorer": {
            "latency_s": round(sc_res["latency_s"], 3),
            "cost_usd": sc_res["cost_usd"],
            "usage": sc_res["usage"],
            "http_status": sc_res["http_status"],
            "error": sc_res["error"],
            "scoring": scoring,
        },
        "total_cost_usd": total_cost,
        "total_latency_s": round((an_res["latency_s"] or 0) + (sc_res["latency_s"] or 0), 3),
    }


async def fetch_vacancies(n: int) -> list[dict]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Vacancy)
                .where(Vacancy.raw_text.isnot(None))
                .order_by(Vacancy.id.desc())
                .limit(n * 3)
            )
        ).scalars().all()
    out = []
    for v in rows:
        if not v.raw_text or len(v.raw_text) < MIN_RAW_LEN:
            continue
        out.append({
            "id": v.id,
            "title": v.title,
            "company_name": v.company_name,
            "raw_text": v.raw_text,
            "raw_len": len(v.raw_text),
        })
        if len(out) >= n:
            break
    return out


async def main():
    runtime = await get_runtime_settings()
    api_key = runtime["openrouter"]["api_key"]
    base_url = runtime["openrouter"]["base_url"]
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing")

    vacancies = await fetch_vacancies(VACANCY_LIMIT)
    print(f"[i] fetched {len(vacancies)} vacancies (avg len "
          f"{sum(v['raw_len'] for v in vacancies)//max(len(vacancies),1)} chars)")

    all_results: list[dict] = []
    for idx, v in enumerate(vacancies, 1):
        print(f"\n==== [{idx}/{len(vacancies)}] vacancy {v['id']}: {v['title']} @ {v['company_name']} "
              f"({v['raw_len']} chars) ====")
        per_vacancy = {"vacancy": {k: v[k] for k in ("id","title","company_name","raw_len")},
                       "results": {}}
        # Run models sequentially to avoid rate-limit weirdness
        for model in MODELS:
            print(f"  -> {model}", flush=True)
            res = await run_model_for_vacancy(
                api_key=api_key, base_url=base_url, model=model, raw_text=v["raw_text"],
            )
            cost = res["total_cost_usd"]
            a = res["analyzer"]; sc = res["scorer"]
            a_tokens = (a.get("usage") or {}).get("total_tokens")
            sc_tokens = (sc.get("usage") or {}).get("total_tokens")
            err_flag = ""
            if a.get("error") or sc.get("error") or a.get("parse_error"):
                err_flag = " [ERR]"
            total = sc["scoring"].get("total_score") if isinstance(sc.get("scoring"), dict) else None
            title = (a.get("analysis") or {}).get("title")
            role = (a.get("analysis") or {}).get("role")
            sen = (a.get("analysis") or {}).get("seniority")
            dom = (a.get("analysis") or {}).get("domains")
            print(f"     cost=${cost:.5f}  lat={res['total_latency_s']:.2f}s  "
                  f"tok A/S={a_tokens}/{sc_tokens}  score={total}  "
                  f"role={role}  sen={sen}  dom={dom}{err_flag}")
            per_vacancy["results"][model] = res
        all_results.append(per_vacancy)

    out_file = OUT_DIR / "results.json"
    out_file.write_text(json.dumps(all_results, ensure_ascii=False, indent=2, default=str))
    print(f"\n[i] saved raw results to {out_file}")

    # -- Summary ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print(" SUMMARY")
    print("=" * 78)
    for model in MODELS:
        costs, lats, errs, parses = [], [], 0, 0
        for pv in all_results:
            r = pv["results"][model]
            if r["total_cost_usd"] is not None:
                costs.append(r["total_cost_usd"])
            lats.append(r["total_latency_s"])
            if r["analyzer"].get("parse_error"): parses += 1
            if r["analyzer"].get("error") or r["scorer"].get("error"): errs += 1
        tot_cost = sum(costs)
        avg_cost = tot_cost / max(len(costs), 1)
        avg_lat = sum(lats) / max(len(lats), 1)
        print(f"{model:<40}  total=${tot_cost:.4f}  avg=${avg_cost:.5f}/vac  "
              f"lat_avg={avg_lat:.2f}s  errs={errs}  parse_err={parses}")


if __name__ == "__main__":
    asyncio.run(main())
