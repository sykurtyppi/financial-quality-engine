#!/usr/bin/env python3
"""KPI-drift validation harness. Phase 2 default = deterministic baseline.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_kpi_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.kpi_validation import run_validation, summarize
from app.services.narrative.kpi_adjudicator import DeterministicAdjudicator


def main() -> int:
    results = run_validation(adjudicator=DeterministicAdjudicator())
    for r in results:
        if r.error:
            print(f"  [{r.group:8s}] {r.name:16s} ERROR — {r.error}"); continue
        fire = "FIRE" if r.fired else "----"
        lead = f" lead={r.lead_days}d" if r.lead_days is not None else ""
        kpis = (" " + ",".join(r.material_kpis)) if r.material_kpis else ""
        print(f"  [{r.group:8s}] {r.name:16s} {fire} pairs={r.n_pairs}{lead}{kpis}")

    s = summarize(results)
    print(f"\n{'=' * 60}\nDETERMINISTIC BASELINE (Phase 2)")
    print(f"  restater recall: {s['restater_fired']}/{s['restater_n']} "
          f"({s['restater_recall']:.0%})  [target reference ~60%]")
    print(f"  clean FP rate:   {s['clean_fired']}/{s['clean_n']} "
          f"({s['clean_fp_rate']:.0%})  [target reference ~18%]")
    print(f"  leads (days): {s['leads_days']}")
    print(f"  early-warning leads (>=135d): {s['early_warning_leads']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
