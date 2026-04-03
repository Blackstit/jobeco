#!/usr/bin/env python3
"""
One-off: clear or fix company logos that point at ATS/job-board favicons.

Run inside the app environment (e.g. docker exec jobeco-admin-web python scripts/backfill_company_logos.py).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from jobeco.db.session import SessionLocal
from jobeco.db.models import Company
from jobeco.processing.company_branding import brand_favicon_url, sanitize_logo_url


async def main() -> None:
  async with SessionLocal() as s:
    rows = (await s.execute(select(Company))).scalars().all()
    updated = 0
    for c in rows:
      if not c.logo_url:
        continue
      clean = sanitize_logo_url(c.logo_url)
      if clean:
        if clean != c.logo_url:
          c.logo_url = clean
          updated += 1
        continue
      # Bad favicon or ATS image: try corporate website
      replacement = brand_favicon_url(c.website)
      if replacement != c.logo_url:
        c.logo_url = replacement
        updated += 1
    await s.commit()
    print(f"Updated {updated} companies (total scanned: {len(rows)})")


if __name__ == "__main__":
  asyncio.run(main())
