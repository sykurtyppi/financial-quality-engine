#!/usr/bin/env python3
"""One-command full analysis report for a ticker: fundamentals + EDGAR
documents -> markdown report under reports/.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/generate_report.py NVDA
    ... generate_report.py NVDA --no-docs     # fundamentals only (faster)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import fetch_dataset
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.reporting.markdown_report import render

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--no-docs", action="store_true", help="skip document ingestion")
    parser.add_argument("--quarters", type=int, default=8)
    args = parser.parse_args()
    ticker = args.ticker.upper()

    dataset, diag = fetch_dataset(ticker, n_quarters=args.quarters)
    print(f"{ticker}: field coverage {diag.coverage():.0%}"
          + (f"; warnings: {'; '.join(diag.warnings)}" if diag.warnings else ""))

    if not args.no_docs:
        client = SecClient()
        docs = fetch_documents(client, ticker, client.company_facts(ticker), n_filings=8)
        dataset.documents = docs.documents
        print(f"documents: {len(docs.documents)} "
              f"({sum(1 for d in docs.documents if d.doc_type.value == 'mdna')} MD&A, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'risk_factors')} risk factors, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'earnings_release')} releases)")
        if docs.diagnostics:
            print(f"document diagnostics: {len(docs.diagnostics)} (sections not found etc.)")

    result = analyze(dataset)
    report = render(result, generated_on=date.today().isoformat())
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{ticker}_{date.today().isoformat()}.md"
    out.write_text(report)
    overall = result.overall.score if result.overall else None
    print(f"overall: {overall} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
