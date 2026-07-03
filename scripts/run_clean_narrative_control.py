#!/usr/bin/env python3
"""Clean-company narrative control — does the narrative layer discriminate?

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_clean_narrative_control.py

Compares per-detector firing rates on clean companies vs the restatement set.
The signal lives where restaters >> clean; the noise is where they match.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.clean_narrative_control import RESTATEMENT_RATES, run_clean_control


def main() -> int:
    results = run_clean_control()
    clean = [r for r in results if r.error is None and not r.has_402]
    n = len(clean)

    for r in results:
        if r.error:
            print(f"  {r.company.name:18s} ERROR — {r.error}"); continue
        if r.has_402:
            print(f"  {r.company.name:18s} EXCLUDED — had a 4.02 (not clean)"); continue
        kinds = ", ".join(sorted(r.independent_kinds)) or "(none)"
        print(f"  {r.company.name:18s} [{r.company.sector:15s}] docs={r.n_documents:2d} | {kinds}")

    print(f"\n{'=' * 72}\nDETECTOR FIRING RATES — restatement set vs clean control (n_clean={n})")
    print(f"{'detector':28s} {'restaters':>12s} {'clean':>10s} {'discriminates?':>16s}")
    all_kinds = sorted(set(RESTATEMENT_RATES) | {k for r in clean for k in r.independent_kinds})
    for kind in all_kinds:
        rn, rd = RESTATEMENT_RATES.get(kind, (0, 10))
        r_rate = rn / rd
        c_count = sum(1 for r in clean if kind in r.independent_kinds)
        c_rate = c_count / n if n else 0.0
        gap = r_rate - c_rate
        verdict = "SIGNAL" if gap >= 0.30 else ("weak" if gap >= 0.15 else "noise")
        print(f"{kind:28s} {rn}/{rd} ({r_rate:>4.0%}) {c_count:>4}/{n} ({c_rate:>4.0%}) {verdict:>16s}")

    # The sharpest hypothesis: high-severity emergence should be ~0 on clean.
    hs = sum(1 for r in clean if "high_severity_disclosure" in r.independent_kinds)
    print(f"\n  KEY: high_severity_disclosure — restaters 3/10 (30%), clean {hs}/{n} "
          f"({hs/n:.0%} if n else 0). If clean ~0%, this is the real discriminating signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
