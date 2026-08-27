#!/usr/bin/env python3
"""One-command full analysis report for a ticker: fundamentals + EDGAR
documents -> markdown report under reports/.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/generate_report.py NVDA
    ... generate_report.py NVDA --no-docs     # fundamentals only (faster)
    ... generate_report.py NVDA --fresh       # bypass caches (filing day)

Report assembly lives in app/services/reporting/report_builder.build_report, the
single builder shared by the CLI, journal, and API (review finding 1).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import fetch_dataset_snapshot
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.reporting.report_builder import build_report

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--no-docs", action="store_true", help="skip document ingestion")
    parser.add_argument("--quarters", type=int, default=8)
    parser.add_argument(
        "--fresh", action="store_true",
        help="bypass EDGAR caches (use on filing days; a <24h cache can serve pre-filing data)",
    )
    args = parser.parse_args()
    ticker = args.ticker.upper()

    client = SecClient(fresh=args.fresh)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    snapshot = fetch_dataset_snapshot(ticker, n_quarters=args.quarters, client=client)
    dataset, diag = snapshot.dataset, snapshot.diagnostics
    print(f"{ticker}: field coverage {diag.coverage():.0%}"
          + (f"; warnings: {'; '.join(diag.warnings)}" if diag.warnings else ""))

    doc_diagnostics: list[str] = []
    if not args.no_docs:
        docs = fetch_documents(client, ticker, snapshot.company_facts, n_filings=8)
        dataset.documents = docs.documents
        doc_diagnostics = list(docs.diagnostics)
        print(f"documents: {len(docs.documents)} "
              f"({sum(1 for d in docs.documents if d.doc_type.value == 'mdna')} MD&A, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'risk_factors')} risk factors, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'earnings_release')} releases)")

    result = analyze(dataset)
    generated_on = date.today().isoformat()
    report, thermometer = build_report(
        result, dataset,
        generated_on=generated_on,
        coverage=diag.coverage(),
        client=client,
        ticker=ticker,
        fetched_at=fetched_at,
        fresh=args.fresh,
        warnings=diag.warnings,
        doc_diagnostics=doc_diagnostics,
        company_facts=snapshot.company_facts,
    )

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{ticker}_{generated_on}.md"
    out.write_text(report)

    # Review finding 8: no 0-100 number on any surface, stdout included.
    if thermometer.reading is None:
        distress = "insufficient data"
    elif thermometer.regime_flags:
        distress = "regime signals present (" + ", ".join(f.code for f in thermometer.regime_flags) + ")"
    else:
        hot = thermometer.hottest_cluster
        distress = f"most-elevated dimension {hot.name}" if hot else "no acute signals"
    print(f"distress signals: {distress} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
