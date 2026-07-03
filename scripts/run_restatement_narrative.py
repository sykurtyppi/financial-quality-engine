#!/usr/bin/env python3
"""Narrative-contradiction test on the restatement (4.02) cases.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_restatement_narrative.py

Does the narrative layer catch what the metrics missed? Separates metric-gated
mismatches (structurally can't fire where metrics missed) from independent
narrative detectors (the only part that could add signal).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.restatement_narrative import run


def main() -> int:
    results = run()
    scorable = [r for r in results if r.error is None]

    for r in results:
        tag = "PURE-FORENSIC" if r.case.healthy_at_time else "distressed-too"
        print(f"\n=== {r.case.name} ({tag}) · 4.02 {r.event_date}")
        if r.error:
            print(f"    ERROR — {r.error}"); continue
        print(f"    pre-4.02 documents: {r.n_documents} over {r.doc_periods}")
        if r.independent_findings:
            print("    INDEPENDENT narrative findings (could catch what metrics missed):")
            for kind, detail in r.independent_findings:
                print(f"      - {kind}: {detail}")
        else:
            print("    INDEPENDENT narrative findings: NONE")
        if r.mismatches:
            print(f"    metric-gated mismatches: {r.mismatches}")
        else:
            print("    metric-gated mismatches: none")

    pure = [r for r in scorable if r.case.healthy_at_time]
    print(f"\n{'=' * 64}\nNARRATIVE-CONTRADICTION on RESTATEMENT CASES — SUMMARY")
    print(f"  scorable: {len(scorable)}/{len(results)}")
    if scorable:
        any_indep = sum(1 for r in scorable if r.has_independent_signal)
        any_mm = sum(1 for r in scorable if r.mismatches)
        print(f"  had ANY independent narrative finding: {any_indep}/{len(scorable)} "
              f"({any_indep/len(scorable):.0%})")
        print(f"  had ANY metric-gated mismatch: {any_mm}/{len(scorable)} "
              f"({any_mm/len(scorable):.0%})")
    if pure:
        pin = sum(1 for r in pure if r.has_independent_signal)
        print(f"\n  PURE-FORENSIC subset (n={len(pure)}) — the real test:")
        print(f"    had independent narrative finding pre-4.02: {pin}/{len(pure)} ({pin/len(pure):.0%})")
    # High-severity emergence is the sharpest independent signal.
    hs = [r.case.name for r in scorable
          if any(k == "high_severity_disclosure" for k, _ in r.independent_findings)]
    print(f"\n  high-severity term emergence (material weakness / going concern / restatement) fired for: "
          f"{hs or 'NONE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
