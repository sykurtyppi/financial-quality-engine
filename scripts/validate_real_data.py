#!/usr/bin/env python3
"""v0.2 real-data validation harness.

Fetches companyfacts for a cross-sector set of tickers, maps them, runs the
full analysis pipeline, and prints per-company diagnostics: field coverage,
extraction methods, warnings, and pipeline outcome. Used to produce
docs/real_data_validation.md.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/validate_real_data.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import fetch_dataset

TICKERS = [
    ("AAPL", "Technology Hardware"),
    ("MSFT", "Software"),
    ("NVDA", "Semiconductors"),
    ("KO", "Consumer Staples"),
    ("CAT", "Industrials"),
    ("XOM", "Energy"),
    ("WMT", "Retail"),
    ("CRM", "Software (SaaS)"),
]


def main() -> int:
    for ticker, sector in TICKERS:
        print(f"\n{'=' * 72}\n{ticker} ({sector})\n{'=' * 72}")
        try:
            dataset, diag = fetch_dataset(ticker, n_quarters=8, sector=sector)
        except Exception as e:  # noqa: BLE001 - validation harness reports everything
            print(f"  INGESTION FAILED: {e}")
            traceback.print_exc(limit=2)
            continue

        print(f"  entity: {diag.entity_name}")
        print(f"  quarters: {dataset.periods[0].fiscal_label} .. {dataset.periods[-1].fiscal_label}")
        print(f"  overall field coverage: {diag.coverage():.0%}")
        for w in diag.warnings:
            print(f"  WARNING: {w}")

        gaps = [f for f in diag.fields if f.periods_filled < f.periods_total]
        full = [f for f in diag.fields if f.periods_filled == f.periods_total]
        derived = {
            f.field_name: f.methods
            for f in diag.fields
            if any(m in f.methods for m in ("ytd_diff", "fy_minus_3q", "composite"))
        }
        print(f"  fully covered fields: {len(full)}/{len(diag.fields)}")
        if derived:
            print("  derivation methods used:")
            for name, methods in sorted(derived.items()):
                print(f"    {name}: {methods}")
        if gaps:
            print("  fields with gaps:")
            for f in sorted(gaps, key=lambda f: f.periods_filled):
                print(
                    f"    {f.field_name}: {f.periods_filled}/{f.periods_total} "
                    f"(tag: {f.tag_used}) missing={f.missing_periods}"
                    + (f" notes={f.notes}" if f.notes else "")
                )

        try:
            result = analyze(dataset)
        except Exception as e:  # noqa: BLE001
            print(f"  PIPELINE FAILED: {e}")
            traceback.print_exc(limit=2)
            continue
        ok = sum(1 for m in result.metrics if m.status.value == "ok")
        miss = sum(1 for m in result.metrics if m.status.value == "missing_data")
        nm = sum(1 for m in result.metrics if m.status.value == "not_meaningful")
        overall = result.overall.score if result.overall else None
        print(f"  pipeline: {ok} ok / {miss} missing / {nm} not_meaningful metrics; overall={overall}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
