#!/usr/bin/env python3
"""Kill-gate validation for the distress thermometer (P1-C).

Question the roadmap requires answering before the composite can be retired:
does the thermometer discriminate distress BETTER than the 0-100 composite?

Method: threshold-free AUC (Mann-Whitney: probability a random stress_case
outranks a random clean control) on the distressed-control backtest cohort,
computed for the composite (`overall`) and for the 2-cluster thermometer built
from the per-component concern columns already in the backtest CSV.

The CSV has no raw NI/EBITDA, so the regime dummies (NI<0, NI<0 x2, EBITDA<0)
are recovered from cached companyfacts at each row's latest_period, PIT-sliced
(periods on/before latest_period), and scored with the SAME `_regime_flags`
logic the live thermometer uses. Reported both ways: cluster-only (a
conservative lower bound) and regime-inclusive (the real thermometer). The
regime-sign of a past quarter is robust to the non-PIT cache snapshot.

    python scripts/validate_thermometer.py [data/backtest/backtest_results.csv]
"""

from __future__ import annotations

import csv
import glob
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.scoring.thermometer import MIN_CLUSTER_MEMBERS, _regime_flags

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
    # Review finding 5: mirror the live MIN_CLUSTER_MEMBERS rule — a cluster
    # with fewer than 2 present members is insufficient, not scored.
    vals = [_num(row[c]) for c in cols if _num(row.get(c, "")) is not None]
    return sum(vals) / len(vals) if len(vals) >= MIN_CLUSTER_MEMBERS else None


def cluster_base(row: dict) -> float | None:
    """2-cluster AOM (max across cluster means), regime dummies excluded."""
    means = [
        m
        for m in (_cluster_mean(row, BALANCE_SHEET), _cluster_mean(row, CASH_GENERATION))
        if m is not None
    ]
    return max(means) if means else None


def _load_raw_facts(tickers: set[str]) -> dict:
    """ticker -> raw companyfacts JSON, for per-row PIT reconstruction."""
    raw: dict[str, dict] = {}
    for tk in tickers:
        files = glob.glob(f"data/cache/companyfacts_{tk}.json")
        if files:
            try:
                raw[tk] = json.load(open(files[0]))
            except Exception as e:  # noqa: BLE001
                # A CORRUPT cache (not a missing one) silently deflates the AUC
                # for this ticker; surface it so the operator knows regime flags
                # were dropped for it (review finding).
                print(f"WARNING: failed to parse cache for {tk}: {e}", file=sys.stderr)
                continue
    return raw


def _regime_add(row: dict, raw: dict) -> float:
    # Review finding 3: TRUE point-in-time. Filter raw facts by the row's `asof`
    # FILING date before mapping, so later amendments/comparatives cannot leak
    # into a historical observation's NI/EBITDA regime flags.
    facts = raw.get(row["ticker"])
    asof = row.get("asof", "")
    if not facts or not asof:
        return 0.0
    try:
        pit_facts = filter_as_of(facts, date.fromisoformat(asof))
        ds, _ = build_dataset(pit_facts, row["ticker"], n_quarters=28)
    except Exception:  # noqa: BLE001 - unmappable PIT window contributes no regime
        return 0.0
    periods = sorted(ds.periods, key=lambda p: p.period_end)
    return sum(f.concern_add for f in _regime_flags(periods))


def thermometer_from_row(row: dict, raw: dict) -> float | None:
    base = cluster_base(row)
    if base is None:
        return None
    return min(100.0, base + _regime_add(row, raw))


def auc(positives: list[float], negatives: list[float]) -> float:
    """P(random positive > random negative); ties count 0.5."""
    if not positives or not negatives:
        return float("nan")
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def _company_median(rows: list[dict], score_fn) -> list[float]:
    """One value per COMPANY (median of its quarters) to avoid the pseudo-
    replication that treating 95/425 company-quarters as independent creates."""
    by_ticker: dict[str, list[float]] = {}
    for r in rows:
        v = score_fn(r)
        if v is not None:
            by_ticker.setdefault(r["ticker"], []).append(v)
    out = []
    for vals in by_ticker.values():
        s = sorted(vals)
        out.append(s[len(s) // 2])
    return out


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/backtest/backtest_results.csv")
    rows = [r for r in csv.DictReader(path.open()) if r["overall"] not in ("", "None")]
    stress = [r for r in rows if r["archetype"] == "stress_case"]
    control = [r for r in rows if r["archetype"].startswith("control_")]
    raw = _load_raw_facts({r["ticker"] for r in stress + control})
    reproducible = bool(raw)  # regime dummies need the git-ignored cache

    n_s, n_c = len({r["ticker"] for r in stress}), len({r["ticker"] for r in control})
    comp_row = lambda r: float(r["overall"])  # noqa: E731
    th_row = lambda r: thermometer_from_row(r, raw)  # noqa: E731

    # Company-level (primary — honest n) and company-quarter (secondary).
    comp_co = auc(_company_median(stress, comp_row), _company_median(control, comp_row))
    th_co = auc(_company_median(stress, th_row), _company_median(control, th_row))
    comp_q = auc([comp_row(r) for r in stress], [th for r in control if (th := comp_row(r)) is not None])
    th_q = auc(
        [t for r in stress if (t := th_row(r)) is not None],
        [t for r in control if (t := th_row(r)) is not None],
    )

    print("=== EXPERIMENTAL — indicative only, NOT a formal validation ===")
    print(f"cohort: {n_s} stress companies ({len(stress)} quarters) vs "
          f"{n_c} control companies ({len(control)} quarters)")
    print("company-quarters are pseudo-replicated (same firm, many quarters);")
    print("AUC on them understates uncertainty. Company-level AUC is primary.\n")
    print(f"composite   AUC: company-level {comp_co:.3f} | company-quarter {comp_q:.3f}")
    print(f"thermometer AUC: company-level {th_co:.3f} | company-quarter {th_q:.3f}")
    if not reproducible:
        print("\nNOTE: regime dummies need data/cache/*.json (git-ignored, absent here),")
        print("so the thermometer figures above are NOT reproducible from a clean checkout.")
    print("\nCaveats: n=6 stress companies; in-sample; cohort is qualitative evidence")
    print("(universe.py: 'not proof'); clustering/config chosen ex-post. This does NOT")
    print("justify retiring the composite — treat the thermometer as experimental until")
    print("out-of-sample and reference-class (P2-E) validation exist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
