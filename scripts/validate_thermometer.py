#!/usr/bin/env python3
"""Kill-gate validation for the distress thermometer (P1-C).

Question the roadmap requires answering before the composite can be retired:
does the thermometer discriminate distress BETTER than the 0-100 composite?

Method: threshold-free AUC (Mann-Whitney: probability a random stress_case
outranks a random clean control) on the distressed-control backtest cohort,
computed for the composite (`overall`) and for the 2-cluster thermometer built
from the per-component concern columns already in the backtest CSV.

Caveat, stated up front: the CSV has no raw NI/EBITDA, so this thermometer is
computed WITHOUT the regime dummies — a conservative lower bound. Regime
dummies target acute distress and would need a pipeline re-run to include.

    python scripts/validate_thermometer.py [data/backtest/backtest_results.csv]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

BALANCE_SHEET = (
    "c_net_debt_to_ebitda",
    "c_interest_coverage",
    "c_current_ratio",
    "c_debt_to_assets",
    "c_leverage_change",
)
CASH_GENERATION = (
    "c_cfo_to_net_income",
    "c_fcf_margin",
    "c_fcf_margin_trend",
    "c_issuance_pressure",
)


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _cluster_mean(row: dict, cols: tuple[str, ...]) -> float | None:
    vals = [_num(row[c]) for c in cols if _num(row.get(c, "")) is not None]
    return sum(vals) / len(vals) if vals else None


def thermometer_from_row(row: dict) -> float | None:
    """2-cluster AOM (max across cluster means), regime dummies excluded."""
    means = [
        m
        for m in (_cluster_mean(row, BALANCE_SHEET), _cluster_mean(row, CASH_GENERATION))
        if m is not None
    ]
    return max(means) if means else None


def auc(positives: list[float], negatives: list[float]) -> float:
    """P(random positive > random negative); ties count 0.5."""
    if not positives or not negatives:
        return float("nan")
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/backtest/backtest_results.csv")
    rows = [r for r in csv.DictReader(path.open()) if r["overall"] not in ("", "None")]
    stress = [r for r in rows if r["archetype"] == "stress_case"]
    control = [r for r in rows if r["archetype"].startswith("control_")]

    comp = auc(
        [float(r["overall"]) for r in stress],
        [float(r["overall"]) for r in control],
    )
    th = auc(
        [t for r in stress if (t := thermometer_from_row(r)) is not None],
        [t for r in control if (t := thermometer_from_row(r)) is not None],
    )

    print(f"cohort: {len(stress)} stress_case vs {len(control)} controls")
    print(f"composite   AUC = {comp:.3f}")
    print(f"thermometer AUC = {th:.3f}  (regime dummies excluded — conservative)")
    verdict = "PASSES" if th > comp + 0.02 else "TIE/FAIL"
    print(f"kill gate ('discriminate better'): {verdict} -> "
          f"{'retire composite' if verdict == 'PASSES' else 'composite stays'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
