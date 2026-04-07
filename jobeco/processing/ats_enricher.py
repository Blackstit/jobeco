"""
ATS Enricher — fetches the full job description from the company's
Applicant Tracking System page (Ashby, Greenhouse, Lever, Workday, etc.)
and returns clean text suitable for LLM analysis.

Strategy (per ATS):
  - Lever:     Public JSON API at api.lever.co (fastest, most reliable)
  - Workable:  Public JSON API v2 at apply.workable.com/api/v2
  - Ashby:     JSON-LD in HTML (schema.org/JobPosting)
  - Workday:   JSON-LD in HTML
  - Greenhouse: HTML body text extraction
  - Hibob:     Pure SPA — try API fallback, else skip
  - Generic:   JSON-LD → body text fallback
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger()

MAX_CHARS = 8000
_TIMEOUT = 25

_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
}

_JSON_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

_LEVER_RE = re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]+)", re.I)
_HIBOB_RE = re.compile(r"\.careers\.hibob\.com/", re.I)
_WORKABLE_RE = re.compile(r"apply\.workable\.com/([^/]+)/j/([A-Za-z0-9]+)", re.I)


def _html_to_text(html_str: str) -> str:
    """Convert HTML fragment to plain text."""
    soup = BeautifulSoup(html_str, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Lever (JSON API) ────────────────────────────────────────────────

async def _fetch_lever_api(apply_url: str) -> str | None:
    """Use Lever's public JSON API — fast and reliable."""
    m = _LEVER_RE.search(apply_url)
    if not m:
        return None
    company, job_id = m.group(1), m.group(2)
    api_url = f"https://api.lever.co/v0/postings/{company}/{job_id}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            resp = await client.get(api_url)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as exc:
        log.debug("lever_api_error", url=api_url, error=str(exc))
        return None

    parts = []

    if data.get("descriptionPlain"):
        parts.append(data["descriptionPlain"].strip())

    for lst in data.get("lists", []):
        heading = lst.get("text", "").strip()
        content = lst.get("content", "")
        if heading and content:
            plain = _html_to_text(content)
            if plain:
                parts.append(f"\n{heading}:\n{plain}")

    if data.get("additionalPlain"):
        parts.append(f"\nAdditional:\n{data['additionalPlain'].strip()}")

    cats = data.get("categories", {})
    meta_parts = []
    if cats.get("commitment"):
        meta_parts.append(f"Type: {cats['commitment']}")
    if cats.get("location"):
        meta_parts.append(f"Location: {cats['location']}")
    if cats.get("team"):
        meta_parts.append(f"Team: {cats['team']}")
    if data.get("workplaceType"):
        meta_parts.append(f"Workplace: {data['workplaceType']}")
    if meta_parts:
        parts.append("\n" + " | ".join(meta_parts))

    result = "\n".join(parts).strip()
    if len(result) > 200:
        log.debug("ats_enriched_lever_api", url=apply_url, chars=len(result))
        return result[:MAX_CHARS]
    return None


# ─── Hibob (SPA fallback) ────────────────────────────────────────────

async def _fetch_hibob(apply_url: str) -> str | None:
    """Hibob is a pure SPA; try the internal API endpoint."""
    parsed = urlparse(apply_url)
    path_parts = parsed.path.rstrip("/").split("/")
    job_id = path_parts[-1] if path_parts else ""
    if not job_id:
        return None

    company_host = parsed.hostname or ""
    api_url = f"https://{company_host}/api/careers/job/{job_id}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={
            **_HEADERS, "accept": "application/json"
        }) as client:
            resp = await client.get(api_url)
            if resp.status_code != 200:
                log.debug("hibob_api_non200", url=api_url, status=resp.status_code)
                return None
            data = resp.json()
    except Exception as exc:
        log.debug("hibob_api_error", url=api_url, error=str(exc))
        return None

    parts = []
    if data.get("title"):
        parts.append(f"Position: {data['title']}")
    if data.get("description"):
        parts.append(_html_to_text(data["description"]))
    if data.get("requirements"):
        parts.append(f"\nRequirements:\n{_html_to_text(data['requirements'])}")
    if data.get("location"):
        loc = data["location"]
        if isinstance(loc, dict):
            loc = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        parts.append(f"\nLocation: {loc}")

    result = "\n".join(parts).strip()
    if len(result) > 200:
        log.debug("ats_enriched_hibob_api", url=apply_url, chars=len(result))
        return result[:MAX_CHARS]
    return None



# ─── Workable (JSON API) ─────────────────────────────────────────────

async def _fetch_workable_api(apply_url: str) -> str | None:
    """Use Workable's public JSON API v2."""
    m = _WORKABLE_RE.search(apply_url)
    if not m:
        return None
    company, shortcode = m.group(1), m.group(2)
    api_url = f"https://apply.workable.com/api/v2/accounts/{company}/jobs/{shortcode}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={
            **_HEADERS, "accept": "application/json"
        }) as client:
            resp = await client.get(api_url)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as exc:
        log.debug("workable_api_error", url=api_url, error=str(exc))
        return None

    parts = []

    if data.get("title"):
        parts.append(f"Position: {data['title']}")

    if data.get("description"):
        parts.append(_html_to_text(data["description"]))

    if data.get("requirements"):
        parts.append(f"\nRequirements:\n{_html_to_text(data['requirements'])}")

    if data.get("benefits"):
        parts.append(f"\nBenefits:\n{_html_to_text(data['benefits'])}")

    meta_parts = []
    if data.get("location"):
        loc = data["location"]
        if isinstance(loc, dict):
            loc = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        meta_parts.append(f"Location: {loc}")
    if data.get("remote"):
        meta_parts.append("Remote: Yes")
    dept = data.get("department")
    if dept:
        meta_parts.append(f"Department: {dept}")
    emp_type = data.get("employment_type")
    if emp_type:
        meta_parts.append(f"Type: {emp_type}")
    if meta_parts:
        parts.append("\n" + " | ".join(meta_parts))

    result = "\n".join(parts).strip()
    if len(result) > 200:
        log.debug("ats_enriched_workable_api", url=apply_url, chars=len(result))
        return result[:MAX_CHARS]
    return None


# ─── JSON-LD extraction ──────────────────────────────────────────────

def _extract_jsonld_description(html: str) -> str | None:
    """Extract JobPosting description from JSON-LD blocks."""
    for m in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            is_job = item.get("@type") in ("JobPosting", "jobPosting")
            if not is_job:
                graph = item.get("@graph", [])
                for g in (graph if isinstance(graph, list) else []):
                    if isinstance(g, dict) and g.get("@type") in ("JobPosting", "jobPosting"):
                        item = g
                        is_job = True
                        break

            if not is_job:
                continue

            desc = item.get("description", "")
            if not desc:
                continue

            parts = [_html_to_text(desc)]

            if item.get("responsibilities"):
                parts.append("\nResponsibilities:\n" + _html_to_text(item["responsibilities"]))
            if item.get("qualifications"):
                parts.append("\nQualifications:\n" + _html_to_text(item["qualifications"]))
            if item.get("skills"):
                parts.append("\nSkills:\n" + _html_to_text(item["skills"]))
            if item.get("experienceRequirements"):
                parts.append(f"\nExperience: {item['experienceRequirements']}")

            salary = item.get("baseSalary") or item.get("estimatedSalary")
            if isinstance(salary, dict):
                val = salary.get("value", {})
                if isinstance(val, dict):
                    lo = val.get("minValue", "")
                    hi = val.get("maxValue", "")
                    cur = salary.get("currency", "USD")
                    if lo or hi:
                        parts.append(f"\nSalary: {cur} {lo}-{hi}")

            return "\n".join(parts)[:MAX_CHARS]
    return None


# ─── Body text fallback ──────────────────────────────────────────────

def _extract_body_text(html: str) -> str:
    """Fallback: extract readable text from the full page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "iframe"]):
        tag.decompose()

    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if not main:
        main = soup.find("article") or soup.find(class_=re.compile(r"job|posting|description|content", re.I))
    target = main or soup.body or soup

    text = target.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"Apply.*?this position|Apply now|Submit.*?application", "", text, flags=re.I)
    return text.strip()[:MAX_CHARS]


# ─── Main entry point ────────────────────────────────────────────────

async def fetch_ats_description(apply_url: str) -> str | None:
    """
    Fetch the apply URL and extract the full job description.
    Routes to the optimal strategy per ATS platform.
    Returns cleaned text or None on failure.
    """
    if not apply_url or not apply_url.startswith("http"):
        return None

    # ── Lever: use JSON API (fast, avoids 700KB+ HTML) ──
    if _LEVER_RE.search(apply_url):
        result = await _fetch_lever_api(apply_url)
        if result:
            return result

    # ── Workable: use JSON API v2 ──
    if _WORKABLE_RE.search(apply_url):
        result = await _fetch_workable_api(apply_url)
        if result:
            return result

    # ── Hibob: pure SPA, try internal API ──
    if _HIBOB_RE.search(apply_url):
        result = await _fetch_hibob(apply_url)
        if result:
            return result
        log.debug("ats_hibob_spa_skip", url=apply_url)
        return None

    # ── Generic: fetch HTML, try JSON-LD then body text ──
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(apply_url)
            if resp.status_code != 200:
                log.debug("ats_fetch_non200", url=apply_url, status=resp.status_code)
                return None
            html = resp.text
    except Exception as exc:
        log.debug("ats_fetch_error", url=apply_url, error=str(exc))
        return None

    if len(html) < 500:
        return None

    text = _extract_jsonld_description(html)
    if text and len(text) > 200:
        log.debug("ats_enriched_jsonld", url=apply_url, chars=len(text))
        return text

    text = _extract_body_text(html)
    if text and len(text) > 200:
        log.debug("ats_enriched_body", url=apply_url, chars=len(text))
        return text

    return None
