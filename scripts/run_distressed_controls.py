#!/usr/bin/env python3
"""Distressed-survivor control for the survivorship miss test.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_distressed_controls.py

Compares the elevated-rate of near-death SURVIVORS (at peak distress) against the
dead set (83% >=p80, 75% >=p90). Similar rates => the engine detects distress,
not death. A gap => real discrimination.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.distressed_controls import run_controls
from app.services.backtesting.survivorship import P80, P90, band

DEAD_P80, DEAD_P90, DEAD_N = 10, 9, 12  # from docs/survivorship_pilot.md


def main() -> int:
    results = run_controls()
    scorable = [r for r in results if r.peak_score is not None]
    e80 = sum(1 for r in scorable if r.peak_score >= P80)
    e90 = sum(1 for r in scorable if r.peak_score >= P90)

    for r in results:
        print(f"\n=== {r.survivor.name} ({r.survivor.ticker}) · {r.survivor.sector} · "
              f"anchor {r.survivor.anchor_date} SIC{r.sic}")
        print(f"    {r.survivor.note}")
        if r.excluded_financial:
            print("    EXCLUDED — financial institution.")
            continue
        if r.error:
            print(f"    ERROR — {r.error}")
            continue
        for h in r.horizons:
            if h.overall is not None:
                print(f"    A-{h.months_before:>2}mo ({h.asof}): overall={h.overall:5.1f} "
                      f"[{band(h.overall)}] cov={h.coverage:.0%} red_flags={h.n_red_flags} · {h.top_blocks}")
            else:
                print(f"    A-{h.months_before:>2}mo ({h.asof}): {h.status}")
        if r.peak_score is not None:
            print(f"    >> peak distress score: {r.peak_score:.1f} ({band(r.peak_score)})")

    print(f"\n{'=' * 64}\nDISTRESSED-SURVIVOR CONTROL — SUMMARY")
    print(f"  scorable survivors: {len(scorable)}/{len(results)}")
    if scorable:
        print(f"  elevated >= p80 at peak distress: {e80}/{len(scorable)} ({e80 / len(scorable):.0%})")
        print(f"  elevated >= p90 at peak distress: {e90}/{len(scorable)} ({e90 / len(scorable):.0%})")
    print(f"\n  DEAD set (reference): >=p80 {DEAD_P80}/{DEAD_N} (83%), >=p90 {DEAD_P90}/{DEAD_N} (75%)")
    print("  Interpretation: if survivor rates ~ dead rates -> distress detection, not death.")
    print("  A meaningful gap (dead >> survivors) -> genuine discrimination.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
