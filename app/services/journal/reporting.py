"""Report generation shared by the CLI report script and the web UI.

Fetches fundamentals + EDGAR documents, runs the pipeline, and writes the
markdown report under ``reports/``. Requires the ``EDGAR_IDENTITY`` env var
(SEC fair-access rule) at call time.

Uses the single shared report builder (review finding 1), so the journal/web UI
gets the same decision card + offerings + restatements + Tier-1 events as the
CLI — not the bare appendix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.core.pipeline import analyze
from app.services.ingestion.edgar_adapter import (
    fetch_dataset_snapshot,
    fetch_submissions_snapshot,
    replay_snapshot,
    store_vintage_snapshot,
)
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.journal.store import safe_ticker
from app.services.reporting.report_builder import build_report as build_full_report
from app.services.reporting.report_builder import ledger_path
from app.services.scoring.thermometer import describe

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports"


def replay_banner(as_of: date, source: str, rebuilt_on: date) -> str:
    """The first thing a replayed report says: what it is, and what it is not."""
    return (
        f"> **HISTORICAL REPLAY — as of {as_of}.** Rebuilt on {rebuilt_on} from "
        f"{source}. Every evidence stream is cut at {as_of} (filed on or before). "
        "It is not the report a reader saw that day: the code is today's, and "
        "documents, offerings and 8-K events can only see filings that today's "
        "filing index (SEC's recent-filings block) still lists."
    )


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
    vintage: bool = True,
    replay: bool = False,
) -> tuple[Path, str]:
    """Generate and write the markdown report for ``ticker``.

    Returns (path, distress_summary). The 0-100 composite is retired from every
    surface (it measured non-discriminating in both directions on the live
    season); the second element is now the thermometer's one-line summary.

    ``fresh`` bypasses the EDGAR cache — required on a filing night, where a
    <24h cached answer can silently predate the filing being waited on.
    ``out_dir``/``banner`` exist for the automatic (non-journal) track: the
    banner is prepended verbatim so an auto-generated artifact can never be
    mistaken for a blind journal case. ``vintage`` archives the scored
    companyfacts payload to the vintage store (the silent-revision baseline);
    a failure there is a data-quality line, never an aborted report.

    ``report_day`` also pins the silent-revision baseline: on the journal track
    it IS the locked entry's day (watch.py hands it to ``journal.py report
    --date``), so the report diffs the newest snapshot against the one taken
    at or before the lock. It still never sets ``generated_on`` (below).

    ``replay`` rebuilds the report AS OF ``report_day`` instead (historical
    replay): fundamentals from the newest vintage snapshot captured by then,
    else today's payload cut there; documents filed by then; every evidence
    stream cut there (they all anchor on ``generated_on``, which a replay sets
    to that day). Nothing is archived, the report opens with a replay banner
    and is written to ``<TICKER>_<day>.replay.md``, never over a real report.
    """
    ticker = ticker.upper()
    as_of: date | None = None
    replay_source = ""
    if replay:
        if not report_day:
            raise ValueError("a historical replay needs the day to replay (report_day)")
        as_of = date.fromisoformat(report_day)
    client = SecClient(fresh=fresh)
    if as_of is not None:
        snapshot, replay_source = replay_snapshot(client, ticker, as_of, n_quarters=quarters)
        vintage_note: str | None = "not captured (historical replay)"
    else:
        snapshot = fetch_dataset_snapshot(ticker, n_quarters=quarters, client=client)
        vintage_note = store_vintage_snapshot(
            client, ticker, snapshot.company_facts, enabled=vintage
        )
    dataset, diag = snapshot.dataset, snapshot.diagnostics
    submissions = fetch_submissions_snapshot(ticker, client)
    doc_diagnostics: list[str] = []
    if with_docs:
        docs = fetch_documents(
            client, ticker, snapshot.company_facts, n_filings=8, submissions=submissions,
            before=as_of,
        )
        dataset.documents = docs.documents
        doc_diagnostics = list(docs.diagnostics)
    result = analyze(dataset)
    # Review finding 1 (round 2): the report is built from CURRENTLY fetched
    # fundamentals/evidence, so it must be labeled with the ACTUAL generation
    # date, never a historical `report_day`. `report_day` only names the output
    # file (to match the journal entry). A true historical replay would require
    # PIT fundamentals + `filed <= as_of` across every stream (the pit.py path),
    # which this regeneration does not do — a replay (below) does.
    generated_on = (as_of or date.today()).isoformat()
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    target_dir = out_dir if out_dir is not None else REPORTS
    suffix = ".replay.md" if replay else ".md"
    out = target_dir / f"{safe_ticker(ticker)}_{report_day or date.today().isoformat()}{suffix}"
    warnings = list(diag.warnings)
    if as_of is not None:
        warnings.append(f"HISTORICAL REPLAY as of {as_of}: fundamentals from {replay_source}.")
    report, thermometer = build_full_report(
        result, dataset,
        generated_on=generated_on,
        coverage=diag.coverage(),
        # The evidence must name the same series the score came from.
        field_tags=diag.selected_series(),
        client=client,
        ticker=ticker,
        fetched_at=fetched_at,
        warnings=warnings,
        field_notes=diag.field_notes(),
        doc_diagnostics=doc_diagnostics,
        company_facts=snapshot.company_facts,
        submissions=submissions,
        index_degraded=submissions is None,
        fresh=fresh,  # the data-quality line must not call a fresh fetch cache-eligible
        vintage_note=vintage_note,
        baseline_day=date.fromisoformat(report_day) if report_day else None,
        # The same claims as data, each with the filings behind it.
        ledger_out=ledger_path(out),
    )
    if as_of is not None:
        report = f"{replay_banner(as_of, replay_source, date.today())}\n\n{report}"
    if banner:
        report = f"{banner}\n\n{report}"
    target_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    return out, describe(thermometer)
