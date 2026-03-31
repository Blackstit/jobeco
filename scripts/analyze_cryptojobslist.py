#!/usr/bin/env python3
"""
Quick analysis of the CryptoJobsList RSS feed.
Fetches recent items and prints a human-readable summary to help
decide whether this source is worth integrating.
"""
import xml.etree.ElementTree as ET
import re
import html
from collections import Counter

import httpx

RSS_URL = "https://api.cryptojobslist.com/rss.xml"
SAMPLE = 30  # only look at the latest N items

# Namespaces in the feed
NS = {
  "dc": "http://purl.org/dc/elements/1.1/",
  "content": "http://purl.org/rss/1.0/modules/content/",
  "media": "http://search.yahoo.com/mrss/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

def strip_html(raw: str) -> str:
  text = _TAG_RE.sub(" ", raw)
  text = html.unescape(text)
  return _WS_RE.sub(" ", text).strip()


CONTACT_PATTERNS = [
  re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                    # email
  re.compile(r"https?://(?:t\.me|telegram\.me)/\S+", re.I),   # telegram
  re.compile(r"@\w{3,}", re.I),                                # @handle
  re.compile(r"https?://(?:apply|jobs|careers)\.\S+", re.I),   # ATS links
  re.compile(r"https?://\S*lever\.co/\S+", re.I),
  re.compile(r"https?://\S*greenhouse\.io/\S+", re.I),
  re.compile(r"https?://\S*workable\.com/\S+", re.I),
  re.compile(r"https?://\S*ashbyhq\.com/\S+", re.I),
  re.compile(r"https?://forms\.gle/\S+", re.I),
]


def extract_contacts(text: str) -> list[str]:
  found = []
  for pat in CONTACT_PATTERNS:
    for m in pat.finditer(text):
      found.append(m.group())
  return list(set(found))


def main():
  print(f"Fetching {RSS_URL} ...")
  resp = httpx.get(RSS_URL, timeout=30)
  resp.raise_for_status()

  root = ET.fromstring(resp.content)
  items = root.findall(".//item")
  print(f"Total items in feed: {len(items)}")
  print(f"Analyzing first {SAMPLE}...\n")

  companies = Counter()
  has_salary = 0
  has_contacts = 0
  has_long_desc = 0
  contact_types = Counter()
  total_desc_len = 0
  locations = Counter()
  sample_items = []

  for item in items[:SAMPLE]:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    company = (item.findtext("dc:creator", namespaces=NS) or "").strip()
    pub_date = (item.findtext("pubDate") or "").strip()
    desc_raw = (item.findtext("description") or "")
    content_raw = (item.findtext("content:encoded", namespaces=NS) or "")

    full_raw = content_raw or desc_raw
    text = strip_html(full_raw)
    desc_text = strip_html(desc_raw)

    companies[company] += 1
    total_desc_len += len(text)

    if len(text) > 500:
      has_long_desc += 1

    # salary detection
    salary_match = bool(re.search(r"\$[\d,]+|\d+k\s*[-–]\s*\d+k|USD|salary|compensation", text, re.I))
    if salary_match:
      has_salary += 1

    # contacts
    contacts = extract_contacts(text)
    if link:
      contacts.append(f"listing:{link}")
    if contacts:
      has_contacts += 1
    for c in contacts:
      if "listing:" in c:
        contact_types["listing_url"] += 1
      elif "@" in c and "." in c and not c.startswith("@"):
        contact_types["email"] += 1
      elif "t.me" in c or "telegram" in c:
        contact_types["telegram"] += 1
      elif c.startswith("@"):
        contact_types["handle"] += 1
      elif "lever.co" in c or "greenhouse" in c or "workable" in c or "ashby" in c:
        contact_types["ats_link"] += 1
      else:
        contact_types["other_url"] += 1

    # location from title (common pattern: "... at Company")
    loc_match = re.search(r"(remote|usa|uk|europe|singapore|hong kong|london|new york)", title, re.I)
    if loc_match:
      locations[loc_match.group(1).lower()] += 1

    sample_items.append({
      "title": title,
      "company": company,
      "link": link,
      "date": pub_date,
      "desc_len": len(text),
      "contacts": contacts,
      "has_salary": salary_match,
    })

  # ── Report ──
  sep = "─" * 60
  print(sep)
  print("FEED ANALYSIS REPORT")
  print(sep)
  print(f"Items analyzed:     {SAMPLE}")
  print(f"Avg description:    {total_desc_len // SAMPLE:,} chars")
  print(f"Long desc (>500c):  {has_long_desc}/{SAMPLE} ({has_long_desc*100//SAMPLE}%)")
  print(f"Mentions salary:    {has_salary}/{SAMPLE} ({has_salary*100//SAMPLE}%)")
  print(f"Has contacts/link:  {has_contacts}/{SAMPLE} ({has_contacts*100//SAMPLE}%)")
  print()

  print("Contact types found:")
  for ct, n in contact_types.most_common():
    print(f"  {ct:20s} {n}")
  print()

  print(f"Top companies ({len(companies)} unique):")
  for comp, n in companies.most_common(10):
    print(f"  {comp:30s} {n}")
  print()

  print("Locations mentioned:")
  for loc, n in locations.most_common():
    print(f"  {loc:20s} {n}")
  print()

  print(sep)
  print("SAMPLE LISTINGS (first 10)")
  print(sep)
  for i, it in enumerate(sample_items[:10], 1):
    contacts_str = ", ".join(c for c in it["contacts"] if "listing:" not in c) or "—"
    print(f"\n{i}. {it['title']}")
    print(f"   Company:  {it['company']}")
    print(f"   Link:     {it['link']}")
    print(f"   Date:     {it['date']}")
    print(f"   Desc len: {it['desc_len']} chars")
    print(f"   Salary:   {'yes' if it['has_salary'] else 'no'}")
    print(f"   Contacts: {contacts_str}")

  print(f"\n{sep}")
  print("VERDICT NOTES")
  print(sep)
  if has_contacts == SAMPLE:
    print("✅ Every item has at least a listing URL (the job page link).")
  else:
    print(f"⚠️  Only {has_contacts}/{SAMPLE} items have contacts.")

  direct = contact_types.get("email", 0) + contact_types.get("telegram", 0) + contact_types.get("ats_link", 0)
  print(f"   Direct contacts (email/tg/ATS): {direct} across {SAMPLE} items")

  if has_salary < SAMPLE // 2:
    print(f"⚠️  Low salary mention rate: {has_salary}/{SAMPLE}")
  else:
    print(f"✅ Salary info present in {has_salary}/{SAMPLE} items")

  if total_desc_len // SAMPLE > 2000:
    print(f"⚠️  Descriptions are very long (avg {total_desc_len // SAMPLE} chars) — high LLM token cost")
  elif total_desc_len // SAMPLE > 500:
    print(f"✅ Descriptions are detailed (avg {total_desc_len // SAMPLE} chars)")
  else:
    print(f"⚠️  Descriptions are short (avg {total_desc_len // SAMPLE} chars)")


if __name__ == "__main__":
  main()
