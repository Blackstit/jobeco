"""
Backfill script: normalize role and seniority for all existing vacancies.
Run inside the admin-web container:
  python3 scripts/backfill_normalize.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobeco.db.session import SessionLocal
from jobeco.db.models import Vacancy
from jobeco.processing.normalization import normalize_vacancy_fields
from sqlalchemy import select


async def main():
    updated_role = 0
    updated_sen = 0
    total = 0

    async with SessionLocal() as s:
        rows = (await s.execute(select(Vacancy))).scalars().all()
        total = len(rows)
        print(f"Processing {total} vacancies...")

        for v in rows:
            norm_role, norm_sen = normalize_vacancy_fields(
                role=v.role,
                seniority=v.seniority,
                title=v.title,
                standardized_title=v.standardized_title,
            )

            changed = False
            if norm_role and norm_role != v.role:
                v.role = norm_role
                updated_role += 1
                changed = True
            elif not v.role and norm_role:
                v.role = norm_role
                updated_role += 1
                changed = True

            if norm_sen and norm_sen != v.seniority:
                v.seniority = norm_sen
                updated_sen += 1
                changed = True
            elif not v.seniority and norm_sen:
                v.seniority = norm_sen
                updated_sen += 1
                changed = True

        await s.commit()

    print(f"Done. {total} vacancies processed.")
    print(f"  Roles normalized: {updated_role}")
    print(f"  Seniority normalized: {updated_sen}")


if __name__ == "__main__":
    asyncio.run(main())
