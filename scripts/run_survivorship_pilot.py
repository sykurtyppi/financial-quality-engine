#!/usr/bin/env python3
"""Run the survivorship-corrected miss test on delisted companies.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_survivorship_pilot.py

The v0.3 miss test could only see survivors. This scores companies that DIED,
using CIK-direct companyfacts that persist on EDGAR post-delisting. Question:
on companies that actually failed, did the engine elevate BEFORE the event?
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.survivorship import P80, P90, band, run_pilot


def main() -> int:
    results = run_pilot()
    scored = [r for r in results if not r.excluded_financial]
    elevated_p80 = elevated_p90 = 0

    for r in results:
        print(f"\n=== {r.company.name} (event {r.company.event_date} · {r.company.event_type}) SIC{r.sic}")
        print(f"    {r.company.note}")
        if r.excluded_financial:
            print("    EXCLUDED — financial institution (engine does not score these).")
            continue
        for h in r.horizons:
            if h.overall is not None:
                tag = band(h.overall)
                print(f"    T-{h.months_before:>2}mo ({h.asof}): overall={h.overall:5.1f} [{tag}] "
                      f"cov={h.coverage:.0%} red_flags={h.n_red_flags} · {h.top_blocks}")
            else:
                print(f"    T-{h.months_before:>2}mo ({h.asof}): {h.status}")
        best = r.best_pre_event_score
        if best is not None:
            if best >= P90:
                elevated_p90 += 1
            if best >= P80:
                elevated_p80 += 1
            print(f"    >> best pre-event score: {best:.1f} ({band(best)})")
        else:
            print("    >> no scorable pre-event quarter (data gap)")

    scorable = [r for r in scored if r.best_pre_event_score is not None]
    print(f"\n{'=' * 64}\nSURVIVORSHIP PILOT SUMMARY")
    print(f"  companies: {len(results)} ({len(results) - len(scored)} excluded financial, "
          f"{len(scored) - len(scorable)} unscorable data gaps)")
    if scorable:
        print(f"  elevated >= p80 pre-event: {elevated_p80}/{len(scorable)} "
              f"({elevated_p80 / len(scorable):.0%})")
        print(f"  elevated >= p90 pre-event: {elevated_p90}/{len(scorable)} "
              f"({elevated_p90 / len(scorable):.0%})")
    print("  Reference: in the general large-cap sweep, 13.5% of names flag >= p90 "
          "(the base rate this must beat to mean anything).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
