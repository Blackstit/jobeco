"""
Standalone scraper for wantapply.com — fetches latest N vacancies
from the main listing page, parses each job page for details,
and saves structured JSON output.

Usage:
    python scripts/scrape_wantapply.py [--limit 15] [--output data/wantapply.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://wantapply.com"

_HEADERS = {
    "accept": "text/html,application/xhtml+xml",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
}

_SLUG_RE = re.compile(r'["\s/]([\w][\w-]*?-at-[\w-]+)')
_OG_COMPANY_RE = re.compile(r" at (.+?)(?:\s*-\s*Wantapply\.com)?$")
_SALARY_RE = re.compile(
    r"(?:(\d[\d,. ]*)\s*-\s*(\d[\d,. ]*))\s*([$€£]|USD|EUR|GBP)"
    r"|"
    r"(\d[\d,. ]*)\s*([$€£]|USD|EUR|GBP)"
    r"|"
    r"([$€£])\s*(\d[\d,. ]*)\s*(?:-\s*(\d[\d,. ]*))?",
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers=_HEADERS)
    resp.raise_for_status()
    return resp.text


async def fetch_listing_slugs(client: httpx.AsyncClient) -> list[str]:
    html = await _fetch(client, BASE_URL + "/")
    slugs: list[str] = []
    seen: set[str] = set()
    for m in _SLUG_RE.finditer(html):
        s = m.group(1)
        if s not in seen and "-at-" in s:
            seen.add(s)
            slugs.append(s)
    return slugs


def _parse_salary(soup: BeautifulSoup) -> dict:
    result: dict = {}
    for p in soup.find_all("p", class_=re.compile(r"text-primary")):
        txt = _clean(p.get_text())
        if txt and len(txt) < 100 and re.search(r"\d", txt):
            result["salary_raw"] = txt
            break
    return result


def _extract_seniority(text: str) -> str | None:
    levels = ["Junior", "Middle", "Senior", "Lead", "Head", "Chief", "Principal", "Staff"]
    found = []
    for lvl in levels:
        if re.search(rf"\b{lvl}\b", text, re.I):
            found.append(lvl)
    return ", ".join(found) if found else None


def _extract_work_type(text: str) -> str | None:
    types = []
    if re.search(r"\bRemote\b", text, re.I):
        types.append("Remote")
    if re.search(r"\bHybrid\b", text, re.I):
        types.append("Hybrid")
    if re.search(r"\bOn-site\b", text, re.I):
        types.append("On-site")
    if re.search(r"\bRelocation\b", text, re.I):
        types.append("Relocation")
    return ", ".join(types) if types else None


async def parse_job_page(client: httpx.AsyncClient, slug: str) -> dict | None:
    url = f"{BASE_URL}/{slug}"
    try:
        html = await _fetch(client, url)
    except httpx.HTTPStatusError as e:
        print(f"  [ERROR] {slug}: HTTP {e.response.status_code}", file=sys.stderr)
        return None

    soup = BeautifulSoup(html, "html.parser")
    data: dict = {"slug": slug, "url": url}

    h1 = soup.find("h1")
    data["title"] = _clean(h1.get_text()) if h1 else None

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title:
        og_val = og_title.get("content", "")
        m = _OG_COMPANY_RE.search(og_val)
        if m:
            data["company"] = m.group(1).strip()

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc:
        data["meta_description"] = og_desc.get("content", "")[:500]

    salary_info = _parse_salary(soup)
    if salary_info:
        data.update(salary_info)

    contact_emails: list[str] = []
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        email = a["href"].replace("mailto:", "").split("?")[0].strip()
        if email and "wantapply.com" not in email.lower():
            contact_emails.append(email)

    raw_html = str(soup)
    for em in re.findall(r"mailto:([\w.+-]+@[\w-]+\.[\w.]+)", raw_html):
        if "wantapply.com" not in em.lower() and em not in contact_emails:
            contact_emails.append(em)

    all_text = soup.get_text(" ", strip=True)
    for em in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", all_text):
        if "wantapply.com" not in em.lower() and em not in contact_emails:
            if not re.match(r".*\.(js|css|png|svg|jpg|webp)$", em):
                contact_emails.append(em)
    if contact_emails:
        data["contact_email"] = contact_emails[0]

    for p in soup.find_all("p", class_=re.compile(r"text-muted|text-sm")):
        txt = _clean(p.get_text())
        m2 = re.search(r"Published on:\s*(.+)", txt)
        if m2:
            data["published_date"] = m2.group(1).strip()
            break

    for el in soup.find_all(string=re.compile(r"See all \d+ jobs? at")):
        txt = _clean(el)
        m = re.search(r"See all \d+ jobs? at (.+)", txt)
        if m:
            data.setdefault("company", m.group(1).strip())

    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == "Website":
            href = a["href"]
            if href.startswith("http") and "wantapply" not in href:
                data["company_website"] = href
                break

    headings = soup.find_all(["h1", "h2", "h3", "h4"])
    desc_parts: list[str] = []
    for h in headings:
        htxt = _clean(h.get_text())
        if not htxt or htxt in (data.get("title", ""), "Similar jobs"):
            continue
        if any(skip in htxt.lower() for skip in ["sign in", "cookie"]):
            continue
        section_text = []
        for sib in h.find_next_siblings():
            if sib.name in ("h1", "h2", "h3", "h4"):
                break
            txt = _clean(sib.get_text())
            if txt and len(txt) > 5:
                section_text.append(txt)
        if section_text:
            desc_parts.append(f"## {htxt}\n" + "\n".join(section_text))
    data["description"] = "\n\n".join(desc_parts) if desc_parts else None

    full_text = soup.get_text(" ", strip=True)
    seniority = _extract_seniority(full_text[:2000])
    if seniority:
        data["seniority"] = seniority
    work_type = _extract_work_type(full_text[:2000])
    if work_type:
        data["work_type"] = work_type

    location_patterns = [
        r"(?:Cyprus|Georgia|Portugal|Poland|Europe|Worldwide|Remote|"
        r"United States|Germany|Hungary|Bulgaria|Serbia|Mexico|"
        r"Latin America|Asia|Spain|Netherlands|France|UK|"
        r"United Kingdom|Canada|Australia|Israel|Turkey|UAE|"
        r"Singapore|Japan|Brazil|Argentina|India|Czech Republic|"
        r"Romania|Greece|Italy|Austria|Switzerland|Belgium|Sweden|"
        r"Norway|Finland|Denmark|Ireland|Estonia|Latvia|Lithuania|"
        r"Croatia|Slovakia|Slovenia|Malta|Luxembourg)",
    ]
    locations = re.findall("|".join(location_patterns), full_text[:3000], re.I)
    if locations:
        unique_locs = list(dict.fromkeys(loc.strip() for loc in locations))[:5]
        data["locations"] = unique_locs

    return data


async def main(limit: int, output: str):
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        print(f"Fetching listing page...")
        slugs = await fetch_listing_slugs(client)
        print(f"Found {len(slugs)} vacancy slugs, parsing first {limit}...")

        results: list[dict] = []
        for i, slug in enumerate(slugs[:limit]):
            print(f"  [{i+1}/{min(limit, len(slugs))}] {slug}")
            data = await parse_job_page(client, slug)
            if data:
                results.append(data)
            await asyncio.sleep(0.3)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(results)} vacancies to {output}")

    print("\n--- Summary ---")
    for v in results:
        salary = v.get("salary_raw", "-")
        contact = v.get("contact_email", v.get("contact_person", "via wantapply"))
        print(f"  {v.get('company','?'):20s} | {v.get('title','?'):40s} | {salary:15s} | {contact}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape wantapply.com vacancies")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--output", default="data/wantapply.json")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.output))
