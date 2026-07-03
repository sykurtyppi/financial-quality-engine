#!/usr/bin/env python3
"""Analyze backtest results and print the tables used in
docs/calibration_report.md.

    .venv/bin/python scripts/analyze_backtest.py data/backtest/backtest_results.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting import analysis as an
from app.services.backtesting.runner import component_metric_names


def fmt(v, pct=False, digits=3):
    if v is None:
        return "—"
    if pct:
        return f"{v:+.1%}"
    return f"{v:.{digits}f}"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "backtest" / "backtest_results.csv")
    rows = an.load_rows(path)
    ok = an.ok_rows(rows)
    statuses = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print(f"rows={len(rows)} statuses={statuses}\n")

    print("== Overall score quintiles vs 12M relative return ==")
    for q in an.quintile_stats(ok, "overall", "rel_12m"):
        lo, hi = q["score_range"]
        print(f"  Q{q['quintile']} (score {lo:.0f}-{hi:.0f}): mean {fmt(q['mean_outcome'], pct=True)} "
              f"median {fmt(q['median_outcome'], pct=True)} n={q['n']}")

    print("\n== Hit rates (overall > 60 vs 12M rel return < -10%) ==")
    hr = an.hit_rates(ok)
    for k, v in hr.items():
        print(f"  {k}: {fmt(v) if isinstance(v, float) else v}")

    print("\n== Block-level ICs (concern vs outcomes; negative = signal works for returns/margins) ==")
    block_cols = [c for c in rows[0] if c.startswith("blk_")] if rows else []
    outcomes = ["rel_12m", "rel_6m", "op_margin_chg_4q", "fcf_margin_chg_4q", "ni_growth_fwd_4q"]
    for ic in an.component_ics(ok, block_cols, outcomes):
        if ic.outcome in ("rel_12m", "op_margin_chg_4q", "fcf_margin_chg_4q") and ic.n >= an.MIN_SAMPLE:
            print(f"  {ic.signal:35s} vs {ic.outcome:20s} IC={fmt(ic.ic)} n={ic.n}")

    print("\n== Component-level ICs vs 12M rel return (|IC| >= 0.05, n >= 100) ==")
    comp_cols = [f"c_{n}" for n in component_metric_names()]
    ics = an.component_ics(ok, comp_cols, ["rel_12m", "op_margin_chg_4q", "fcf_margin_chg_4q"])
    for ic in sorted(ics, key=lambda x: (x.outcome, x.ic if x.ic is not None else 0)):
        if ic.ic is not None and abs(ic.ic) >= 0.05 and ic.n >= 100:
            print(f"  {ic.signal:38s} vs {ic.outcome:20s} IC={fmt(ic.ic)} n={ic.n}")

    print("\n== Archetype diagnostics ==")
    for d in an.archetype_diagnostics(rows):
        print(f"  {d['archetype']:20s} rows={d['rows']:3d} scored={d['scored']:3d} "
              f"excluded={d['excluded']:2d} stale={d['stale_skips']:2d} "
              f"mean_overall={fmt(d['mean_overall'], digits=1)} "
              f"pct_flagged={fmt(d['pct_flagged'])} fp_among_flagged={fmt(d['fp_rate_among_flagged'])}")

    print("\n== Stale-filing skips (delayed statements = risk event in itself) ==")
    for s in an.stale_skip_signal(rows):
        print(f"  {s['ticker']}: {s['stale_asofs']} as-of dates with stale filings")

    print("\n== Non-reliance (8-K 4.02) events within 24m of an as-of ==")
    nr = [r for r in ok if r.get("non_reliance_24m") == "1"]
    tickers = sorted({r["ticker"] for r in nr})
    print(f"  rows={len(nr)} tickers={tickers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
