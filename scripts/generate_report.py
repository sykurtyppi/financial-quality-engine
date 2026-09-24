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
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import (
    fetch_dataset_snapshot,
    fetch_submissions_snapshot,
    store_vintage_snapshot,
)
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient, SecClientError
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
    parser.add_argument(
        "--no-vintage", action="store_true",
        help="do not archive the scored companyfacts payload to data/vintages/",
    )
    args = parser.parse_args()
    ticker = args.ticker.upper()

    client = SecClient(fresh=args.fresh)
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    snapshot = fetch_dataset_snapshot(ticker, n_quarters=args.quarters, client=client)
    dataset, diag = snapshot.dataset, snapshot.diagnostics
    # Archive exactly the payload that is about to be scored: the baseline a
    # later silent revision would otherwise erase. Failure is a report line.
    vintage_note = store_vintage_snapshot(
        client, ticker, snapshot.company_facts, enabled=not args.no_vintage
    )
    print(f"{ticker}: field coverage {diag.coverage():.0%}"
          + (f"; warnings: {'; '.join(diag.warnings)}" if diag.warnings else ""))

    submissions = fetch_submissions_snapshot(ticker, client)

    doc_diagnostics: list[str] = []
    if not args.no_docs:
        docs = fetch_documents(
            client, ticker, snapshot.company_facts, n_filings=8, submissions=submissions
        )
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
        # The evidence must name the same series the score came from.
        field_tags=diag.selected_tags(),
        client=client,
        ticker=ticker,
        fetched_at=fetched_at,
        fresh=args.fresh,
        warnings=diag.warnings,
        field_notes=diag.field_notes(),
        doc_diagnostics=doc_diagnostics,
        company_facts=snapshot.company_facts,
        submissions=submissions,
        index_degraded=submissions is None,
        vintage_note=vintage_note,
        # No baseline_day: the CLI has no pinned thesis; the silent-revision
        # section compares the newest snapshot with the previous one only.
    )

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{ticker}_{generated_on}.md"
    out.write_text(report)

    # Review finding 8: no 0-100 number on any surface, stdout included.
    from app.services.scoring.thermometer import describe

    print(f"distress signals: {describe(thermometer)} -> {out}")
    return 0


def _main() -> int:
    """`main`, with SEC acquisition failures reported as what they are. The
    fundamentals are the report: when SEC cannot supply them (unreachable,
    throttled, a cache entry that cannot be refetched) there is nothing to
    build, and a traceback told the operator less than the one line below."""
    try:
        return main()
    except SecClientError as e:
        print(f"error: {e}", file=sys.stderr)
        print("no report written: the fundamentals could not be acquired. Retry, or "
              "check EDGAR_IDENTITY and the network.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
