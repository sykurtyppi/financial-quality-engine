#!/usr/bin/env python3
"""Timing analysis for the validated narrative signals — early warning or
contemporaneous?

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_narrative_timing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.narrative_timing import classify, run


def q(days: int | None) -> str:
    return "n/a" if days is None else f"{days}d (~{days/91:.1f}Q)"


def main() -> int:
    results = run()
    hs_leads: list[int] = []
    direct_leads: list[int] = []

    for r in results:
        print(f"\n=== {r.case.name} · 4.02 {r.event_date}")
        if r.error:
            print(f"    ERROR — {r.error}"); continue
        hs_hits = [h for h in r.detector_hits if h.kind == "high_severity_disclosure"]
        kpi_hits = [h for h in r.detector_hits if h.kind == "kpi_definition_change"]
        for h in hs_hits:
            print(f"    high_severity fired {h.period} (filed {h.filed}): "
                  f"lead {q(h.lead_days)} -> {classify(h.lead_days)}")
            if h.lead_days is not None:
                hs_leads.append(h.lead_days)
        for h in kpi_hits:
            print(f"    kpi_definition_change {h.period} (filed {h.filed}): "
                  f"lead {q(h.lead_days)} -> {classify(h.lead_days)}")
        if not hs_hits and not kpi_hits:
            print("    (no tracked-signal findings)")
        if r.direct_term:
            print(f"    direct scan: earliest MD&A/release mention of '{r.direct_term}' "
                  f"filed {r.direct_filed}: lead {q(r.direct_lead_days)} -> {classify(r.direct_lead_days)}")
            if r.direct_lead_days is not None:
                direct_leads.append(r.direct_lead_days)
        else:
            print("    direct scan: no high-severity term in any MD&A/release pre-4.02")

    print(f"\n{'=' * 64}\nTIMING SUMMARY")
    if hs_leads:
        print(f"  high_severity detector leads (days): {sorted(hs_leads)}")
        print(f"    median {sorted(hs_leads)[len(hs_leads)//2]}d; "
              f"early-warning (>= {135}d): {sum(1 for x in hs_leads if x >= 135)}/{len(hs_leads)}")
    if direct_leads:
        print(f"  direct-scan earliest-mention leads (days): {sorted(direct_leads)}")
        print(f"    median {sorted(direct_leads)[len(direct_leads)//2]}d; "
              f"early-warning: {sum(1 for x in direct_leads if x >= 135)}/{len(direct_leads)}")
    print("\n  Early warning = the signal led the 4.02 by >= ~1.5 quarters.")
    print("  Contemporaneous = it emerged within ~one filing cycle (a faithful never-misser).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
