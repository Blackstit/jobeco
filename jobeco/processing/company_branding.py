"""
Helpers to avoid storing ATS / job-board / aggregator favicons as company logos.

Logos are derived via Google favicon service from a domain; if that domain is
Lever, Greenhouse, Remocate, etc., users see the wrong icon.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# Hosts that are never the employer's own brand site (apply platforms, boards, our sources).
_ATS_AND_BOARD_EXACT = frozenset({
  "remocate.app",
  "web3.career",
  "cryptojobs.com",
  "cryptojobslist.com",
  "degencryptojobs.com",
  "wantapply.com",
  "workatastartup.com",
  "cryptocurrencyjobs.co",
  "sailonchain.com",
  "jaabz.com",
  "indeed.com",
  "glassdoor.com",
  "hh.ru",
  "djinni.co",
  "jooble.org",
  "careerjet.com",
  "ziprecruiter.com",
  "monster.com",
  "simplyhired.com",
  "greenhouse.io",
  "lever.co",
  "workable.com",
  "ashbyhq.com",
  "breezy.hr",
  "bamboohr.com",
  "smartrecruiters.com",
  "recruitee.com",
  "teamtailor.com",
  "personio.com",
  "jobvite.com",
  "icims.com",
  "taleo.net",
  "ultipro.com",
  "linkedin.com",
  "wellfound.com",
  "angel.co",
})

_ATS_AND_BOARD_SUFFIXES = (
  "lever.co",
  "greenhouse.io",
  "workable.com",
  "ashbyhq.com",
  "breezy.hr",
  "bamboohr.com",
  "smartrecruiters.com",
  "recruitee.com",
  "teamtailor.com",
  "workday.com",
  "myworkdayjobs.com",
  "successfactors.com",
  "taleo.net",
  "icims.com",
  "jobvite.com",
  "ultipro.com",
  "personio.com",
  "greenhouse.io",
  "oraclecloud.com",
)

# job-boards.greenhouse.io, boards.greenhouse.io
_GREENHOUSE_SUBDOMAIN_RE = re.compile(
  r"^(?:boards|job-boards)\.greenhouse\.io$",
  re.I,
)


def _normalize_host(host: str) -> str:
  h = (host or "").lower().strip().rstrip(".")
  if h.startswith("www."):
    h = h[4:]
  return h


def is_ats_or_job_board_host(host: str) -> bool:
  """True if hostname is an ATS, job board, aggregator, or our scrape source — not employer brand."""
  h = _normalize_host(host)
  if not h or "." not in h:
    return True
  if h in _ATS_AND_BOARD_EXACT:
    return True
  for ex in _ATS_AND_BOARD_EXACT:
    if h.endswith("." + ex):
      return True
  if _GREENHOUSE_SUBDOMAIN_RE.match(h):
    return True
  for suffix in _ATS_AND_BOARD_SUFFIXES:
    if h == suffix or h.endswith("." + suffix):
      return True
  return False


def is_ats_or_job_board_url(url: str | None) -> bool:
  if not url or not isinstance(url, str):
    return True
  try:
    u = url.strip()
    if not u.startswith("http"):
      u = "https://" + u
    parsed = urlparse(u)
    host = parsed.hostname or ""
    return is_ats_or_job_board_host(host)
  except Exception:
    return True


def _gstatic_favicon_target_host(logo_url: str) -> str | None:
  """If URL is a Google favicon proxy, return the target site's host from query param url=."""
  low = logo_url.lower()
  if "gstatic.com" not in low or "favicon" not in low:
    return None
  try:
    q = parse_qs(urlparse(logo_url).query)
    raw = (q.get("url") or q.get("domain") or [None])[0]
    if not raw:
      return None
    inner = unquote(raw)
    if not inner.startswith("http"):
      inner = "https://" + inner.lstrip("/")
    return (urlparse(inner).hostname or "").lower() or None
  except Exception:
    return None


def sanitize_logo_url(logo_url: str | None) -> str | None:
  """
  Return logo URL only if it plausibly represents the employer brand.
  Drops ATS/board favicons and malformed values.
  """
  if not logo_url or not isinstance(logo_url, str):
    return None
  u = logo_url.strip()
  if not u.startswith("http"):
    return None

  low = u.lower()
  if "gstatic.com" in low and "favicon" in low:
    inner_host = _gstatic_favicon_target_host(u)
    if not inner_host or is_ats_or_job_board_host(inner_host):
      return None
    return u

  try:
    host = urlparse(u).hostname or ""
  except Exception:
    return None
  if is_ats_or_job_board_host(host):
    return None
  return u


def brand_favicon_url(company_website: str | None) -> str | None:
  """Google favicon URL for company_website, or None if site is not a safe corporate domain."""
  if not company_website:
    return None
  try:
    u = company_website.strip()
    if not u.startswith("http"):
      u = "https://" + u
    domain = urlparse(u).hostname or ""
    domain = _normalize_host(domain)
    if not domain or "." not in domain:
      return None
    if is_ats_or_job_board_host(domain):
      return None
    return (
      f"https://t2.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&"
      f"fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=128"
    )
  except Exception:
    return None

def pick_corporate_website(*candidates: str | None) -> str | None:
  """First non-empty URL whose host is not an ATS/job board/aggregator."""
  for c in candidates:
    if not c or not isinstance(c, str):
      continue
    u = c.strip()
    if u and not is_ats_or_job_board_url(u):
      return u
  return None
