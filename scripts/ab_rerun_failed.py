"""Rerun analyzer + scorer only for models that parse-failed on first pass."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jobeco.runtime_settings import get_runtime_settings
from scripts.ab_models_test import (
    MODELS,
    run_model_for_vacancy,
    fetch_vacancies,
    VACANCY_LIMIT,
    OUT_DIR,
)

RERUN_MODELS = ["deepseek/deepseek-v3.2", "google/gemini-2.5-flash"]


async def main():
    runtime = await get_runtime_settings()
    api_key = runtime["openrouter"]["api_key"]
    base_url = runtime["openrouter"]["base_url"]

    old = json.loads(Path("/tmp/ab_models/results.json").read_text())
    vacancies = await fetch_vacancies(VACANCY_LIMIT)

    for idx, v in enumerate(vacancies, 1):
        matching = [pv for pv in old if pv["vacancy"]["id"] == v["id"]]
        pv = matching[0] if matching else None
        print(f"\n==== [{idx}/{len(vacancies)}] vac {v['id']}: {v['title']} ({v['raw_len']} chars) ====")
        for model in RERUN_MODELS:
            print(f"  -> {model}", flush=True)
            res = await run_model_for_vacancy(
                api_key=api_key, base_url=base_url, model=model, raw_text=v["raw_text"],
            )
            cost = res["total_cost_usd"]
            a = res["analyzer"]; sc = res["scorer"]
            total = sc["scoring"].get("total_score") if isinstance(sc.get("scoring"), dict) else None
            an = a["analysis"] or {}
            err = " [ERR]" if a.get("error") or sc.get("error") or a.get("parse_error") else ""
            print(f"     cost=${cost:.5f}  lat={res['total_latency_s']:.2f}s  score={total}  "
                  f"role={an.get('role')}  sen={an.get('seniority')}  dom={an.get('domains')}{err}")
            if pv is not None:
                pv["results"][model] = res

    out_file = OUT_DIR / "results.json"
    out_file.write_text(json.dumps(old, ensure_ascii=False, indent=2, default=str))
    print(f"\n[i] updated {out_file}")

    print("\n" + "=" * 78)
    print(" NEW SUMMARY (all 5 models)")
    print("=" * 78)
    for model in MODELS:
        costs, lats, errs, parses = [], [], 0, 0
        for pv in old:
            r = pv["results"].get(model)
            if not r: continue
            if r["total_cost_usd"] is not None:
                costs.append(r["total_cost_usd"])
            lats.append(r["total_latency_s"])
            if r["analyzer"].get("parse_error"): parses += 1
            if r["analyzer"].get("error") or r["scorer"].get("error"): errs += 1
        tot = sum(costs); avg = tot / max(len(costs), 1); avg_lat = sum(lats) / max(len(lats), 1)
        print(f"{model:<42} total=${tot:.4f}  avg=${avg:.5f}/vac  lat={avg_lat:.1f}s  parse_err={parses}/{len(lats)}")


if __name__ == "__main__":
    asyncio.run(main())
