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
from app.services.ingestion.edgar_adapter import fetch_dataset_snapshot
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
    fresh: bool = False,
    out_dir: Path | None = None,
    banner: str | None = None,
) -> tuple[Path, float | None]:
    """Generate and write the markdown report for ``ticker``. Returns (path, overall).

    ``fresh`` bypasses the EDGAR cache — required on a filing night, where a
    <24h cached answer can silently predate the filing being waited on.
    ``out_dir``/``banner`` exist for the automatic (non-journal) track: the
    banner is prepended verbatim so an auto-generated artifact can never be
    mistaken for a blind journal case.
    """
    ticker = ticker.upper()
    client = SecClient(fresh=fresh)
    snapshot = fetch_dataset_snapshot(ticker, n_quarters=quarters, client=client)
    dataset, diag = snapshot.dataset, snapshot.diagnostics
    doc_diagnostics: list[str] = []
    if with_docs:
        docs = fetch_documents(client, ticker, snapshot.company_facts, n_filings=8)
        dataset.documents = docs.documents
        doc_diagnostics = list(docs.diagnostics)
    result = analyze(dataset)
    # Review finding 1 (round 2): the report is built from CURRENTLY fetched
    # fundamentals/evidence, so it must be labeled with the ACTUAL generation
    # date, never a historical `report_day`. `report_day` only names the output
    # file (to match the journal entry). A true historical replay would require
    # PIT fundamentals + `filed <= as_of` across every stream (the pit.py path),
    # which this regeneration does not do.
    generated_on = date.today().isoformat()
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
        company_facts=snapshot.company_facts,
    )
    if banner:
        report = f"{banner}\n\n{report}"
    target_dir = out_dir if out_dir is not None else REPORTS
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{safe_ticker(ticker)}_{report_day or date.today().isoformat()}.md"
    out.write_text(report)
    overall = result.overall.score if result.overall else None
    return out, overall
