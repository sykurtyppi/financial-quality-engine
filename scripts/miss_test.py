#!/usr/bin/env python3
"""Track 2: false-negative miss test.

Scores the PRE-trouble quarters of known accounting/quality blowups that are
still SEC filers, using point-in-time data (facts filed <= as-of only). The
question: did the screen elevate BEFORE the publicly known event?

Reference distribution (v0.3 calibration, 2021-2025, n=1141):
p50 = 31.7, p80 = 40.3, p90 = 45.1. Caveat: pre-2021 score distributions may
differ; comparisons are indicative.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/miss_test.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.ingestion.sec_client import SecClient

P50, P80, P90 = 31.7, 40.3, 45.1

# (ticker, event description, event date, pre-event as-of dates)
CASES = [
    ("KHC", "Feb-2019 $15B writedown, SEC subpoena, later restatement", date(2019, 2, 21),
     [date(2018, 8, 15), date(2018, 11, 15)]),
    ("GE", "Oct-2017 insurance reserve shortfall; Jan-2018 $6.2B charge; SEC probe", date(2017, 10, 20),
     [date(2017, 5, 15), date(2017, 8, 15)]),
    ("UAA", "Late-2016 growth break; SEC probe (disclosed 2019) into 2015-16 revenue pull-forwards", date(2016, 10, 25),
     [date(2016, 5, 15), date(2016, 8, 15)]),
    ("TUP", "2023 going-concern doubt, filing delays, restatement", date(2023, 4, 7),
     [date(2022, 8, 15), date(2022, 11, 15)]),
    ("PLUG", "Mar-2021 non-reliance (8-K 4.02), restatement of FY2018-2020", date(2021, 3, 16),
     [date(2020, 8, 15), date(2020, 11, 15)]),
    ("SMCI", "Aug-2018 filing delinquency; Nov-2018 non-reliance; Nasdaq delisting-relisting", date(2018, 8, 29),
     [date(2017, 8, 15), date(2017, 11, 15)]),
]


def band(score: float) -> str:
    if score >= P90:
        return ">=p90 ELEVATED"
    if score >= P80:
        return ">=p80 elevated"
    if score >= P50:
        return ">=p50"
    return "<p50 QUIET"


def main() -> int:
    client = SecClient()
    for ticker, event, event_date, asofs in CASES:
        print(f"\n=== {ticker} — {event} (event ~{event_date})")
        try:
            trimmed = trim_to_mapped_tags(client.company_facts(ticker))
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {e}")
            continue
        for asof in asofs:
            try:
                ds, diag = build_pit_dataset(trimmed, ticker, asof, n_quarters=8)
            except ValueError as e:
                print(f"  {asof}: PIT build failed ({e})")
                continue
            latest = ds.periods[-1].period_end
            if (asof - latest).days > 150:
                print(f"  {asof}: STALE FILINGS (latest period {latest}) — itself a signal")
                continue
            result = analyze(ds)
            if result.overall is None or result.overall.score is None:
                print(f"  {asof}: no overall score (coverage {diag.coverage():.0%})")
                continue
            s = result.overall.score
            blocks = sorted(
                (b for b in result.block_scores if b.score is not None),
                key=lambda b: -b.score,  # type: ignore[arg-type]
            )[:2]
            top = ", ".join(f"{b.name}={b.score:.0f}" for b in blocks)
            print(
                f"  {asof}: overall={s:.1f} [{band(s)}] coverage={diag.coverage():.0%} "
                f"red_flags={len(result.red_flags)} top_blocks: {top}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
