"""Report generation shared by the CLI report script and the web UI.

Fetches fundamentals + EDGAR documents, runs the pipeline, and writes the
markdown report under ``reports/``. Requires the ``EDGAR_IDENTITY`` env var
(SEC fair-access rule) at call time.

Uses the single shared report builder (review finding 1), so the journal/web UI
gets the same decision card + offerings + restatements + Tier-1 events as the
CLI — not the bare appendix.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import fetch_dataset
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.journal.store import safe_ticker
from app.services.reporting.report_builder import build_report as build_full_report

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports"


def report_path(ticker: str, day: str | None = None) -> Path:
    return REPORTS / f"{safe_ticker(ticker)}_{day or date.today().isoformat()}.md"


def build_report(
    ticker: str,
    with_docs: bool = True,
    quarters: int = 8,
    report_day: str | None = None,
) -> tuple[Path, float | None]:
    """Generate and write the markdown report for ``ticker``. Returns (path, overall)."""
    ticker = ticker.upper()
    client = SecClient()
    dataset, diag = fetch_dataset(ticker, n_quarters=quarters, client=client)
    doc_diagnostics: list[str] = []
    if with_docs:
        docs = fetch_documents(client, ticker, client.company_facts(ticker), n_filings=8)
        dataset.documents = docs.documents
        doc_diagnostics = list(docs.diagnostics)
    result = analyze(dataset)
    generated_on = report_day or date.today().isoformat()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report, _ = build_full_report(
        result, dataset,
        generated_on=generated_on,
        coverage=diag.coverage(),
        client=client,
        ticker=ticker,
        fetched_at=fetched_at,
        warnings=diag.warnings,
        doc_diagnostics=doc_diagnostics,
    )
    REPORTS.mkdir(exist_ok=True)
    out = report_path(ticker, report_day)
    out.write_text(report)
    overall = result.overall.score if result.overall else None
    return out, overall
