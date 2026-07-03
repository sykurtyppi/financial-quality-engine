#!/usr/bin/env python3
"""Restatement (4.02) forensic control — can the engine flag an accounting
problem before the company admits it?

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_restatement_control.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.restatement_control import (
    ACCOUNTING_BLOCKS,
    ACCOUNTING_CONCERN,
    P80,
    P90,
    band,
    run_restatement_control,
)


def main() -> int:
    results = run_restatement_control()
    scorable = [r for r in results if r.best_overall is not None]

    for r in results:
        tag = "PURE-FORENSIC" if r.case.healthy_at_time else "distressed-too"
        print(f"\n=== {r.case.name} ({tag}) · 4.02 {r.event_date} SIC{r.sic}")
        print(f"    {r.case.note}")
        if r.excluded_financial:
            print("    EXCLUDED — financial institution."); continue
        if r.event_date is None:
            print("    no 4.02 found / unscorable."); continue
        for h in r.horizons:
            if h.overall is not None:
                acct = ", ".join(f"{b}={h.blocks.get(b, float('nan')):.0f}" for b in ACCOUNTING_BLOCKS)
                print(f"    T-{h.months_before:>2}mo ({h.asof}): overall={h.overall:5.1f} [{band(h.overall)}] "
                      f"top={h.top_block} | accounting: {acct}")
            else:
                print(f"    T-{h.months_before:>2}mo ({h.asof}): {h.status}")
        bo, ba = r.best_overall, r.best_accounting_block
        driver = "ACCOUNTING-driven" if r.accounting_driven else "not accounting-driven"
        print(f"    >> best overall {bo:.1f} ({band(bo)}); best accounting-block "
              f"{ba:.0f} -> {driver}" if bo is not None else "    >> unscorable")

    pure = [r for r in scorable if r.case.healthy_at_time]
    print(f"\n{'=' * 64}\nRESTATEMENT (4.02) FORENSIC CONTROL — SUMMARY")
    print(f"  scorable: {len(scorable)}/{len(results)}")
    if scorable:
        e90 = sum(1 for r in scorable if r.best_overall >= P90)
        acct = sum(1 for r in scorable if r.accounting_driven)
        print(f"  elevated overall >= p90 pre-restatement: {e90}/{len(scorable)} ({e90/len(scorable):.0%})")
        print(f"  ACCOUNTING-block-driven (Earnings/Revenue Quality >= {ACCOUNTING_CONCERN:.0f}): "
              f"{acct}/{len(scorable)} ({acct/len(scorable):.0%})")
    if pure:
        pe90 = sum(1 for r in pure if r.best_overall >= P90)
        pacct = sum(1 for r in pure if r.accounting_driven)
        print(f"\n  PURE-FORENSIC subset (healthy-at-time, n={len(pure)}) — the real test:")
        print(f"    elevated >= p90: {pe90}/{len(pure)} ({pe90/len(pure):.0%})")
        print(f"    accounting-driven: {pacct}/{len(pure)} ({pacct/len(pure):.0%})")
    print("\n  Reference: general base rate 13.5% >=p90; distress-survivors 70%; dead set 75%.")
    print("  The forensic claim needs the PURE subset to elevate via ACCOUNTING blocks,")
    print("  not distress blocks. If it doesn't, the engine detects distress, not misstatement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
