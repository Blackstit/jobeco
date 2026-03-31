#!/usr/bin/env python3
"""
Quick analysis of cryptocurrencyjobs.co RSS + individual job pages.
Fetches the RSS, then samples a few job pages for deeper inspection.
"""
import xml.etree.ElementTree as ET
import re
import json
import html
from collections import Counter

import httpx

RSS_URL = "https://cryptocurrencyjobs.co/index.xml"
SAMPLE_RSS = 30
SAMPLE_PAGES = 8

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

def strip_html(raw: str) -> str:
  return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw))).strip()


CONTACT_PATTERNS = [
  re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
  re.compile(r"https?://(?:t\.me|telegram\.me)/\S+", re.I),
  re.compile(r"@\w{3,}"),
  re.compile(r"https?://\S*lever\.co/\S+", re.I),
  re.compile(r"https?://\S*greenhouse\.io/\S+", re.I),
  re.compile(r"https?://\S*workable\.com/\S+", re.I),
  re.compile(r"https?://\S*ashbyhq\.com/\S+", re.I),
  re.compile(r"https?://\S*bamboohr\.com/\S+", re.I),
  re.compile(r"https?://\S*smartrecruiters\.com/\S+", re.I),
  re.compile(r"https?://forms\.gle/\S+", re.I),
  re.compile(r"https?://\S*apply\.\S+", re.I),
  re.compile(r"https?://\S*jobs\.\S+", re.I),
  re.compile(r"https?://\S*careers\.\S+", re.I),
]


def extract_contacts(text: str) -> list[str]:
  found = []
  for pat in CONTACT_PATTERNS:
    for m in pat.finditer(text):
      found.append(m.group())
  return list(set(found))


def fetch_job_page(url: str, client: httpx.Client) -> dict:
  """Fetch a job page and extract structured data + raw text."""
  resp = client.get(url, follow_redirects=True)
  page_html = resp.text

  result = {
    "url": url,
    "json_ld": None,
    "page_text_len": 0,
    "contacts": [],
    "has_apply_button": False,
    "apply_url": None,
    "company_links": [],
    "salary_mentioned": False,
  }

  # Extract JSON-LD
  ld_match = re.search(r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>', page_html, re.S)
  if ld_match:
    try:
      result["json_ld"] = json.loads(ld_match.group(1))
    except Exception:
      pass

  # Extract apply link (often a button)
  apply_match = re.search(r'href="([^"]+)"[^>]*>.*?[Aa]pply', page_html, re.S)
  if apply_match:
    result["has_apply_button"] = True
    result["apply_url"] = apply_match.group(1)

  page_text = strip_html(page_html)
  result["page_text_len"] = len(page_text)

  result["contacts"] = extract_contacts(page_text)
  result["salary_mentioned"] = bool(re.search(r"\$[\d,]+|\d+k\s*[-–]\s*\d+k|salary|compensation", page_text, re.I))

  # Company social links from JSON-LD
  if result["json_ld"]:
    org = result["json_ld"].get("hiringOrganization", {})
    result["company_links"] = org.get("sameAs", [])

  return result


def main():
  print(f"Fetching {RSS_URL} ...")
  resp = httpx.get(RSS_URL, timeout=30)
  resp.raise_for_status()

  root = ET.fromstring(resp.content)
  items = root.findall(".//item")
  print(f"Total items in feed: {len(items)}")
  print(f"Analyzing RSS structure (first {SAMPLE_RSS})...\n")

  companies = Counter()
  categories = Counter()
  has_date = 0
  locations_from_desc = Counter()

  for item in items[:SAMPLE_RSS]:
    title_raw = (item.findtext("title") or "").strip()
    desc = (item.findtext("description") or "").strip()
    link = (item.findtext("link") or "").strip()
    pub_date = (item.findtext("pubDate") or "").strip()

    # Extract company from title: "Job Title at Company"
    at_match = re.search(r" at (.+)$", title_raw)
    company = at_match.group(1) if at_match else "?"
    companies[company] += 1

    # Extract category from URL path
    if link:
      parts = link.replace("https://cryptocurrencyjobs.co/", "").split("/")
      if len(parts) >= 1 and parts[0]:
        categories[parts[0]] += 1

    if pub_date:
      has_date += 1

    # Location from description
    loc_match = re.search(r"(?:remote|based in |anywhere in )([^.]+)", desc, re.I)
    if loc_match:
      loc_text = loc_match.group(0).strip().rstrip(".")
      locations_from_desc[loc_text[:50]] += 1

  sep = "─" * 60
  print(sep)
  print("RSS FEED OVERVIEW")
  print(sep)
  print(f"Total items:       {len(items)}")
  print(f"Has pubDate:       {has_date}/{SAMPLE_RSS}")
  print(f"Unique companies:  {len(companies)}")
  print()

  print("Top companies:")
  for c, n in companies.most_common(10):
    print(f"  {c:35s} {n}")
  print()

  print("Categories (from URL path):")
  for c, n in categories.most_common():
    print(f"  {c:25s} {n}")
  print()

  print("Locations from descriptions:")
  for l, n in locations_from_desc.most_common(10):
    print(f"  {l:50s} {n}")
  print()

  # ── Deep dive: fetch actual job pages ──
  print(sep)
  print(f"DEEP DIVE: Fetching {SAMPLE_PAGES} job pages...")
  print(sep)

  pages_with_apply = 0
  pages_with_contacts = 0
  pages_with_salary = 0
  pages_with_jsonld = 0
  apply_domains = Counter()
  total_page_len = 0

  with httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
    for i, item in enumerate(items[:SAMPLE_PAGES]):
      link = (item.findtext("link") or "").strip()
      title = (item.findtext("title") or "").strip()
      if not link:
        continue

      print(f"\n{i+1}. {title}")
      print(f"   URL: {link}")

      try:
        page = fetch_job_page(link, client)
      except Exception as e:
        print(f"   ERROR: {e}")
        continue

      total_page_len += page["page_text_len"]

      if page["json_ld"]:
        pages_with_jsonld += 1
        ld = page["json_ld"]
        org = ld.get("hiringOrganization", {})
        print(f"   JSON-LD: ✅ | Company: {org.get('name', '?')}")
        if ld.get("jobLocation"):
          locs = ld["jobLocation"]
          if isinstance(locs, list):
            for loc in locs:
              addr = loc.get("address", {})
              print(f"   Location: {addr.get('addressLocality', '?')}, {addr.get('addressCountry', '?')}")
        emp_types = ld.get("employmentType", [])
        print(f"   Employment: {', '.join(emp_types) if emp_types else '?'}")
        if org.get("sameAs"):
          print(f"   Company links: {', '.join(org['sameAs'][:3])}")
        if org.get("logo"):
          print(f"   Logo: ✅")
      else:
        print(f"   JSON-LD: ❌")

      if page["has_apply_button"]:
        pages_with_apply += 1
        apply_url = page["apply_url"] or ""
        print(f"   Apply button: ✅  → {apply_url[:80]}")
        # Extract domain
        dm = re.search(r"https?://([^/]+)", apply_url)
        if dm:
          apply_domains[dm.group(1)] += 1
      else:
        print(f"   Apply button: ❌")

      contacts = [c for c in page["contacts"] if "cryptocurrencyjobs" not in c.lower()]
      if contacts:
        pages_with_contacts += 1
        print(f"   Contacts: {', '.join(contacts[:5])}")
      else:
        print(f"   Contacts: —")

      print(f"   Salary mentioned: {'✅' if page['salary_mentioned'] else '❌'}")
      if page["salary_mentioned"]:
        pages_with_salary += 1
      print(f"   Page text: {page['page_text_len']:,} chars")

  print(f"\n{sep}")
  print("SUMMARY")
  print(sep)
  print(f"Pages with JSON-LD:    {pages_with_jsonld}/{SAMPLE_PAGES}")
  print(f"Pages with Apply URL:  {pages_with_apply}/{SAMPLE_PAGES}")
  print(f"Pages with contacts:   {pages_with_contacts}/{SAMPLE_PAGES}")
  print(f"Pages with salary:     {pages_with_salary}/{SAMPLE_PAGES}")
  print(f"Avg page text:         {total_page_len // max(SAMPLE_PAGES,1):,} chars")

  if apply_domains:
    print(f"\nApply URL domains:")
    for d, n in apply_domains.most_common():
      print(f"  {d:40s} {n}")

  print(f"\n{sep}")
  print("VERDICT")
  print(sep)

  pros = []
  cons = []

  if has_date == SAMPLE_RSS:
    pros.append("Every item has a pubDate — easy to track new-only")
  if pages_with_jsonld >= SAMPLE_PAGES * 0.8:
    pros.append(f"JSON-LD on {pages_with_jsonld}/{SAMPLE_PAGES} pages — structured company/location data")
  if pages_with_apply >= SAMPLE_PAGES * 0.5:
    pros.append(f"Apply button on {pages_with_apply}/{SAMPLE_PAGES} pages — can extract ATS links")
  if len(companies) > SAMPLE_RSS * 0.5:
    pros.append(f"High company diversity: {len(companies)} unique in {SAMPLE_RSS} items")

  if pages_with_contacts < SAMPLE_PAGES * 0.3:
    cons.append(f"Direct contacts found on only {pages_with_contacts}/{SAMPLE_PAGES} pages")
  if pages_with_salary < SAMPLE_PAGES * 0.3:
    cons.append(f"Salary info on only {pages_with_salary}/{SAMPLE_PAGES} pages")
  if total_page_len // max(SAMPLE_PAGES,1) > 5000:
    cons.append(f"Pages are large (avg {total_page_len // SAMPLE_PAGES:,} chars)")

  print("\nPros:")
  for p in pros:
    print(f"  ✅ {p}")
  print("\nCons:")
  for c in cons:
    print(f"  ⚠️  {c}")


if __name__ == "__main__":
  main()
