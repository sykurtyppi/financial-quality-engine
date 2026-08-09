#!/usr/bin/env python3
"""One-command full analysis report for a ticker: fundamentals + EDGAR
documents -> markdown report under reports/.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/generate_report.py NVDA
    ... generate_report.py NVDA --no-docs     # fundamentals only (faster)
    ... generate_report.py NVDA --fresh       # bypass caches (filing day)
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
from app.services.ingestion.edgar_adapter import fetch_dataset
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.reporting.markdown_report import render

logging.basicConfig(level=logging.WARNING)


def _data_quality_section(
    fetched_at: str,
    fresh: bool,
    coverage: float,
    warnings: list[str],
    doc_diagnostics: list[str],
    offerings_error: str | None,
    restatements_error: str | None = None,
) -> str:
    """P0-D: acquisition quality belongs in the artifact, not stdout. A fetch
    failure must be distinguishable from 'the filer didn't disclose'."""
    lines = [
        "## Appendix: Data Acquisition Quality",
        "",
        f"- Data fetched: {fetched_at} "
        + ("(caches bypassed)" if fresh else "(EDGAR JSON caches up to 24h old; use --fresh on filing days)"),
        f"- XBRL field coverage: {coverage:.0%}",
    ]
    for w in warnings:
        lines.append(f"- Ingestion warning: {w}")
    for d in doc_diagnostics:
        lines.append(f"- Document acquisition: {d}")
    if offerings_error is not None:
        lines.append(
            f"- **Capital-markets appendix UNAVAILABLE** (fetch/parse failed: {offerings_error}). "
            "Absence of the offerings section is a data gap, not evidence of no activity."
        )
    if restatements_error is not None:
        lines.append(
            f"- **Restatement appendix UNAVAILABLE** (detection failed: {restatements_error}). "
            "Absence of the restatement section is a data gap, not evidence of no revisions."
        )
    return "\n".join(lines)


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

    dataset, diag = fetch_dataset(ticker, n_quarters=args.quarters, client=client)
    print(f"{ticker}: field coverage {diag.coverage():.0%}"
          + (f"; warnings: {'; '.join(diag.warnings)}" if diag.warnings else ""))

    doc_diagnostics: list[str] = []
    if not args.no_docs:
        docs = fetch_documents(client, ticker, client.company_facts(ticker), n_filings=8)
        dataset.documents = docs.documents
        doc_diagnostics = list(docs.diagnostics)
        print(f"documents: {len(docs.documents)} "
              f"({sum(1 for d in docs.documents if d.doc_type.value == 'mdna')} MD&A, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'risk_factors')} risk factors, "
              f"{sum(1 for d in docs.documents if d.doc_type.value == 'earnings_release')} releases)")
        if doc_diagnostics:
            print(f"document diagnostics: {len(doc_diagnostics)} (rendered in report appendix)")

    result = analyze(dataset)
    report = render(result, generated_on=date.today().isoformat())

    # Capital-markets activity: evidence appendix, never a score input
    # (docs/accuracy_improvement_plan.md B1). A failure is rendered in the
    # data-quality appendix, never silently dropped (P0-D).
    offerings_error: str | None = None
    try:
        from app.services.ingestion.offerings import fetch_offerings, render_offerings_section

        timeline = fetch_offerings(client, ticker)
        report += "\n\n" + render_offerings_section(timeline) + "\n"
        if timeline.takedown_count:
            print(f"offerings: {timeline.takedown_count} takedown(s) in last "
                  f"{timeline.lookback_months} months")
    except Exception as e:  # noqa: BLE001 - appendix must never break the report
        offerings_error = str(e)
        print(f"offerings appendix failed: {e}")

    # Prior-period restatements: evidence appendix, never a score input (P0-5).
    # Scoped to recent periods so the live monitor surfaces revisions to the
    # window it analyses, not decade-old reclassifications.
    restatements_error: str | None = None
    try:
        from app.services.ingestion.restatements import (
            detect_restatements,
            render_restatements_section,
        )

        recent_cutoff = date(date.today().year - 3, 1, 1)
        footprints = detect_restatements(
            client.company_facts(ticker), period_since=recent_cutoff
        )
        report += "\n\n" + render_restatements_section(footprints) + "\n"
        amended_n = sum(1 for f in footprints if f.is_amendment)
        if footprints:
            print(f"restatements: {len(footprints)} revised figure(s) since "
                  f"{recent_cutoff}, {amended_n} via amended filings")
    except Exception as e:  # noqa: BLE001 - appendix must never break the report
        # Review finding 8: a detection failure must be visible in the report,
        # not just stdout — readers cannot otherwise tell "no revisions" from
        # "detector failed".
        restatements_error = str(e)
        print(f"restatements appendix failed: {e}")

    report += "\n\n" + _data_quality_section(
        fetched_at, args.fresh, diag.coverage(), diag.warnings, doc_diagnostics,
        offerings_error, restatements_error,
    ) + "\n"

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{ticker}_{date.today().isoformat()}.md"
    out.write_text(report)
    overall = result.overall.score if result.overall else None
    print(f"overall: {overall} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
