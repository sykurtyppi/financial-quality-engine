"""Single report builder shared by CLI, journal, and API (review finding 1).

Before this, the CLI assembled the decision card + evidence streams inline while
the journal and API called ``render()`` directly — so the frontend and API
returned the bare appendix (no card, offerings, restatements, or Tier-1 events)
while the text still claimed a decision card existed. This is the one builder
every surface uses.

With a ``SecClient`` the evidence streams (offerings, restatements, 8-K 4.02
events) are fetched and any acquisition failure is rendered visibly in the
data-quality section — including the event stream (review finding 4), so a
failed fetch never reads as "clean". Without a client (API posts a dataset) the
card + appendix render from the dataset alone.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.schemas.financials import CompanyDataset
from app.schemas.report import AnalysisResult
from app.services.ingestion.payloads import ExternalPayloadError
from app.services.ingestion.sec_client import SecClientError
from app.services.reporting.decision_card import render_decision_card
from app.services.reporting.markdown_report import render
from app.services.scoring.thermometer import DistressThermometer, compute_thermometer

SNAPSHOT_UNAVAILABLE = (
    "filing index could not be read once for this run; each evidence stream "
    "acquired it separately, so sections may reflect different moments"
)


ANALYSIS_SCOPE_NOTICE = (
    "## Scope limitation\n\n"
    "**Examples of material risks not analyzed by this engine include:** purchase "
    "commitments and guarantees, customer concentration, and export controls/"
    "geopolitical exposure. This list is not exhaustive; review the filing and its "
    "notes directly before making an investment decision."
)


def data_quality_section(
    *,
    fetched_at: str,
    fresh: bool,
    coverage: float,
    warnings: list[str],
    doc_diagnostics: list[str],
    offerings_error: StreamFailure | str | None = None,
    restatements_error: StreamFailure | str | None = None,
    events_error: StreamFailure | str | None = None,
    vintage: str | None = None,
    restatement_scan: str | None = None,
    archives: str | None = None,
    field_notes: list[str] | None = None,
    vintage_error: StreamFailure | str | None = None,
    vintage_diff: str | None = None,
) -> str:
    """A fetch failure must be distinguishable from 'the filer didn't disclose'
    (P0-D), for every stream including events (review finding 4).

    `vintage` is what happened to the companyfacts snapshot this run — the
    baseline the silent-revision check diffs against. None means capture was
    not attempted (API path, --no-vintage); a failure is rendered, not hidden.
    `restatement_scan` is the scan's coverage line (which fields were and were
    not inspected for revisions); None means the stream did not run.
    `archives` is the client's own count of filing documents fetched vs served
    from the immutable archive cache — the line says what happened, not what
    the flag asked for. "Caches bypassed" used to be claimed while every MD&A
    and EX-99 came from a cache `--fresh` never touched."""
    lines = [
        "## Appendix: Data Acquisition Quality",
        "",
        f"- Data fetched: {fetched_at} "
        + (
            "(EDGAR JSON caches bypassed)"
            if fresh
            else "(EDGAR JSON caches up to 24h old; use --fresh on filing days)"
        ),
        f"- XBRL field coverage: {coverage:.0%}",
    ]
    if archives is not None:
        lines.append(f"- Filing documents: {archives}")
    if vintage is not None:
        lines.append(f"- Vintage snapshot: {vintage}")
    # What the silent-revision check compared (or why it compared nothing):
    # "no baseline yet" must never read as "no silent revisions".
    if vintage_diff is not None:
        lines.append(f"- Silent-revision check: {vintage_diff}")
    if restatement_scan is not None:
        lines.append(f"- Restatement scan: {restatement_scan}")
    lines += [f"- Ingestion warning: {w}" for w in warnings]
    # How a scored figure was built (mapper notes): a reader who sees
    # "total debt may understate" can weigh the leverage block accordingly.
    lines += [f"- Field note: {n}" for n in field_notes or []]
    lines += [f"- Document acquisition: {d}" for d in doc_diagnostics]
    for label, err, gap in (
        ("Capital-markets", offerings_error, "no activity"),
        ("Restatement", restatements_error, "no revisions"),
        ("Event (8-K 4.02)", events_error, "no events"),
        ("Silent-revision", vintage_error, "no silent revisions"),
    ):
        if err is None:
            continue
        failure = err if isinstance(err, StreamFailure) else StreamFailure("data", str(err))
        if failure.internal:
            lines.append(
                f"- **{label} appendix UNAVAILABLE — internal error** ({failure.message}). "
                "This is a defect in this tool, not a data gap: the section was never "
                f"computed, so its absence is not evidence of {gap}. Please report it."
            )
        else:
            lines.append(
                f"- **{label} appendix UNAVAILABLE** (fetch/parse failed: {failure.message}). "
                f"Absence of that section is a data gap, not evidence of {gap}."
            )
    return "\n".join(lines)


def _archive_summary(client) -> str | None:
    summary = getattr(client, "archive_summary", None)
    return summary() if callable(summary) else None


def _restatement_tier1_lines(footprints) -> list[str]:
    """Review finding 6: one Tier-1 line per restatement EVENT (accession +
    period), not per field — field detail stays in the appendix section.

    AMENDMENTS ONLY, by design (the PR #4 review): a /A filing is the
    high-confidence, low-false-positive Tier-1 signal. Non-amendment same-period
    revisions are lower-confidence (often discontinued-ops / spinoff
    re-presentations) and are shown in the appendix's "Other prior-period
    revisions" subsection, not promoted to the 90-second card."""
    # Key on the AMENDMENT event (round-7), not the form carrying the current
    # value — so a genuine /A that was later superseded by an ordinary filing
    # still promotes to Tier-1.
    by_event: dict[tuple[str, object, str], list[str]] = defaultdict(list)
    for f in footprints:
        if f.is_amendment:
            by_event[(f.amendment_accession, f.period_end, f.amendment_form)].append(f.field_name)
    lines = []
    for (_accn, period_end, form), fields in sorted(
        by_event.items(), key=lambda kv: str(kv[0][1]), reverse=True
    ):
        lines.append(
            f"Restatement ({form}) affecting {period_end}: {len(fields)} figure(s) revised "
            "(detail in appendix)"
        )
    return lines


logger = logging.getLogger(__name__)

# What an evidence stream may fail on without it being a defect here: SEC
# could not be reached (SecClientError), SEC — or a stored SEC snapshot —
# returned a shape the parser rejects (ExternalPayloadError, raised by the
# validated accessors in `ingestion/payloads.py`), or local storage failed
# (OSError). These degrade the stream to a data-gap notice.
#
# Anything else escaping a stream is a defect in this code by construction:
# the parsers check payload shapes, so a TypeError or KeyError no longer
# stands in for "SEC sent something odd". In tests (and with
# FQE_STRICT_STREAMS=1) it propagates and fails the run. In production the
# report still renders — a latent bug must not cost a filing-night report —
# but the stream is labelled an INTERNAL error, never a data gap, the card
# names it, and the traceback is logged.
STREAM_DATA_ERRORS: tuple[type[BaseException], ...] = (
    SecClientError,
    ExternalPayloadError,
    OSError,
)
# None: read FQE_STRICT_STREAMS at call time. Tests set True (conftest).
STRICT_STREAMS: bool | None = None


def _strict() -> bool:
    if STRICT_STREAMS is not None:
        return STRICT_STREAMS
    return os.environ.get("FQE_STRICT_STREAMS") == "1"


@dataclass(frozen=True)
class StreamFailure:
    """Why an evidence stream produced nothing: `data` (a gap in what SEC or
    the store provided) or `internal` (a defect in this tool)."""

    kind: str  # "data" | "internal"
    message: str

    @property
    def internal(self) -> bool:
        return self.kind == "internal"

    def __str__(self) -> str:
        return self.message


def _stream_failure(stream: str, exc: Exception) -> StreamFailure:
    """Classify a stream's exception. Data failures are recorded; anything
    else re-raises under STRICT_STREAMS, and is otherwise logged and recorded
    as an internal error."""
    if isinstance(exc, STREAM_DATA_ERRORS):
        return StreamFailure("data", str(exc))
    if _strict():
        raise exc
    logger.error("evidence stream %r failed with a defect", stream, exc_info=exc)
    return StreamFailure("internal", f"{type(exc).__name__}: {exc}")


@dataclass
class _Staged:
    """One evidence stream's output, held until the stream has finished.
    `failure` is a data failure the stream reports about itself while still
    rendering an honest section (the offerings acquisition error)."""

    sections: list[str] = field(default_factory=list)
    event_lines: list[str] = field(default_factory=list)
    tier1: list[str] = field(default_factory=list)
    result: Any = None
    failure: StreamFailure | None = None


def _collect_streams(
    client,
    ticker: str,
    report_date: date,
    company_facts: dict | None = None,
    submissions: dict | None = None,
    field_tags: Mapping[str, str | None] | None = None,
    baseline_day: date | None = None,
    vintage_root: Path | None = None,
):
    """Fetch offerings, restatements, 8-K 4.02 events and the silent-revision
    diff. Returns (body_sections, event_lines, tier1_events, errors,
    takedowns, scan, vintage_diff).
    `takedowns` is the CLASSIFIED OfferingFiling list (not a count): the
    Capital Integrity caveat must attribute only what the parsed records
    establish — a debt 424B5 or an issuer-primary deal is not a sponsor sale.
    `scan` is the RestatementScan (None when that stream failed): the card
    and the data-quality section need what it did NOT inspect, which the
    section body alone cannot tell them. Stream availability is derived from
    `errors` by the caller — there is no separate list. `vintage_diff` is the
    VintageDiffReport (None when that stream failed); `baseline_day` is the
    pinned thesis day whose snapshot the newest one is also diffed against,
    and `vintage_root` overrides the store location (tests)."""
    body_sections: list[str] = []
    event_lines: list[str] = []
    tier1_events: list[str] = []
    errors: dict[str, StreamFailure | None] = {
        "offerings": None, "restatements": None, "events": None, "vintage": None,
    }

    def run(name: str, build: Callable[[], _Staged]) -> Any:
        """Build one stream into a staging area and commit it only if the
        whole stream succeeded. A stream that raised halfway used to leave
        what it had already appended — a Tier-1 alert, a section — beside a
        line saying the same stream was unavailable (Hermes audit round 3,
        finding 3). Now a failed stream contributes nothing but its failure."""
        try:
            staged = build()
        except Exception as e:  # noqa: BLE001 - a stream must never break the report
            errors[name] = _stream_failure(name, e)
            return None
        body_sections.extend(staged.sections)
        event_lines.extend(staged.event_lines)
        tier1_events.extend(staged.tier1)
        if staged.failure is not None:
            errors[name] = staged.failure
        return staged.result

    def offerings() -> _Staged:
        from app.services.ingestion.offerings import (
            fetch_offerings,
            render_offerings_section,
        )

        out = _Staged(result=[])
        timeline = fetch_offerings(client, ticker, as_of=report_date, submissions=submissions)
        out.sections.append(render_offerings_section(timeline))
        # Review finding 1 (round 5): fetch_offerings swallows a submissions
        # outage into a structured acquisition_error instead of raising, so check
        # it explicitly — otherwise an outage reads as checked-and-clean. The
        # section it rendered says so itself.
        if timeline.acquisition_error is not None:
            out.failure = StreamFailure("data", timeline.acquisition_error)
        elif timeline.takedown_count:
            out.result = list(timeline.takedowns)
            out.event_lines.append(
                f"{timeline.takedown_count} securities takedown(s) in the last "
                f"{timeline.lookback_months} months (see Capital Markets Activity)"
            )
        return out

    def restatements() -> _Staged:
        from app.services.ingestion.restatements import (
            render_restatements_section,
            scan_restatements,
        )

        cutoff = date(report_date.year - 3, 1, 1)
        facts = company_facts if company_facts is not None else client.company_facts(ticker)
        scan = scan_restatements(
            facts, period_since=cutoff, as_of=report_date, selected_tags=field_tags
        )
        out = _Staged(result=scan)
        out.sections.append(render_restatements_section(scan))
        out.tier1 += _restatement_tier1_lines(scan.footprints)
        return out

    def events() -> _Staged:
        from app.services.backtesting.events import fetch_entity_events

        found = fetch_entity_events(client, ticker, submissions=submissions)
        cutoff = date(report_date.year - 2, report_date.month, min(report_date.day, 28))
        nr_dates = _pit_dates(found.non_reliance_8k_dates, cutoff, report_date)
        out = _Staged()
        out.tier1 += [
            f"8-K Item 4.02 non-reliance (restatement announced) filed {d}" for d in nr_dates
        ]
        return out

    def vintage() -> _Staged:
        from app.services.ingestion.vintages import (
            report_diff,
            silent_revision_tier1_lines,
        )

        cik = client.resolve_cik(ticker)
        # Same period window as the restatement section; the Tier-1 floor is
        # the 4.02 window's formula (~8 quarter-ends), so the card promotes
        # only revisions to periods a reader still holds in mind.
        since = date(report_date.year - 3, 1, 1)
        floor = date(report_date.year - 2, report_date.month, min(report_date.day, 28))
        vintage_diff = report_diff(
            cik, as_of=report_date, baseline_day=baseline_day, since=since, root=vintage_root
        )
        out = _Staged(result=vintage_diff)
        out.sections.append(_silent_revisions_section(vintage_diff))
        # Promote from BOTH windows, each fact once. The lock-to-now window
        # catches a revision that landed in an intermediate state (invisible
        # to previous -> newest); previous -> newest catches a revision to a
        # period the lock snapshot did not yet contain (a quarter added after
        # the lock, then quietly revised), which the lock-to-now diff cannot
        # see because it only walks facts present in the older snapshot.
        # `compared` (and a lock window) imply both snapshots exist; the
        # explicit None checks only let the type checker see it.
        newest, previous, baseline = vintage_diff.newest, vintage_diff.previous, vintage_diff.baseline
        windows = []
        if vintage_diff.changes_since_baseline is not None and baseline is not None and newest is not None:
            windows.append((vintage_diff.changes_since_baseline, baseline.captured, newest.captured))
        if vintage_diff.compared and previous is not None and newest is not None:
            windows.append((vintage_diff.changes_since_previous, previous.captured, newest.captured))
        promoted: set[tuple] = set()
        for changes, older, newer in windows:
            fresh = [c for c in changes if (c.field_name, c.key.start, c.key.end) not in promoted]
            out.tier1 += silent_revision_tier1_lines(fresh, older, newer, period_since=floor)
            promoted |= {(c.field_name, c.key.start, c.key.end) for c in changes}
        return out

    takedowns = run("offerings", offerings) or []
    scan = run("restatements", restatements)
    run("events", events)
    vintage_diff = run("vintage", vintage)

    return body_sections, event_lines, tier1_events, errors, takedowns, scan, vintage_diff


def _silent_revisions_section(rep) -> str:
    """Markdown for the Tier-2 (between-snapshot) revision check. Evidence
    framing only — no scoring. Says what was compared before saying what
    moved, and says plainly when nothing could be compared."""
    from app.services.ingestion.vintages import render_changes

    lines = ["## Silent Revisions Between Snapshots (evidence — not scored)", ""]
    if not rep.compared:
        lines.append(
            f"Not checked: {rep.no_baseline_reason}. Two distinct companyfacts "
            f"snapshots taken at or before {rep.as_of} are needed to diff; the store "
            "fills as reports and the watch sweep run, and nothing can be back-filled."
        )
        return "\n".join(lines)
    lines.append(
        "_Prior-period figures that changed or disappeared between the two most "
        f"recent distinct companyfacts snapshots taken at or before {rep.as_of}. "
        "Facts added for new periods are not listed. Nothing here has an amended "
        "filing behind it — read the filing before calling any of it a restatement._"
    )
    lines.append("")
    lines.append(render_changes(rep.changes_since_previous, rep.previous.captured, rep.newest.captured))
    if rep.changes_since_baseline is not None and rep.baseline is not None:
        lines.append(f"**Since the pinned thesis was locked** ({rep.baseline.captured}):")
        lines.append("")
        lines.append(render_changes(rep.changes_since_baseline, rep.baseline.captured, rep.newest.captured))
    elif rep.baseline_note:
        lines.append(f"_{rep.baseline_note}._")
    return "\n".join(lines)


def _selling_stockholder_takedowns(takedowns: list) -> list:
    """Takedowns whose PARSED evidence establishes secondary or mixed
    selling-stockholder participation. A debt 424B5 or an issuer-primary
    equity deal must never be counted here — labeling those "sponsor sales"
    states an attribution the underlying data does not establish. The bar is
    positive classification as EQUITY, not merely "not debt": an "unknown"
    instrument whose boilerplate happens to say "selling stockholders" is
    still an attribution the parse has not established."""
    out = []
    for f in takedowns:
        if getattr(f, "security_type", "unknown") != "equity":
            continue
        if (
            getattr(f, "has_selling_stockholders", False)
            or (getattr(f, "secondary_shares", None) or 0) > 0
            or getattr(f, "company_receives_no_secondary_proceeds", False)
        ):
            out.append(f)
    return out


def _capital_integrity_offerings_caveat(
    result: AnalysisResult, takedowns: list
) -> str | None:
    """FPS-class consistency check (2026Q2's worst miss): Capital Integrity
    scored the sponsor's serial sell-downs 10/100 — lowest concern — because
    the block only sees issuer-side dilution, not selling-stockholder
    takedowns. When the offerings timeline shows SELLING-STOCKHOLDER
    takedowns while the block reads low-concern, say so next to the events
    instead of letting the score quietly contradict the filings. Fires only
    on classified secondary/mixed evidence — never on debt or issuer-primary
    deals, whose sale the block is not blind to. Cross-reference only; no
    score change."""
    secondary = _selling_stockholder_takedowns(takedowns)
    if not secondary:
        return None
    from app.config import scoring_config as cfg

    ci = next((b for b in result.block_scores if b.name == "Capital Integrity"), None)
    if ci is None or ci.score is None or ci.score >= cfg.DIRECTION_POSITIVE_BELOW:
        return None
    return (
        f"CAVEAT — Capital Integrity reads low-concern ({ci.score:.0f}/100) but is "
        f"blind to the {len(secondary)} selling-stockholder takedown(s) above "
        "(secondary/mixed offerings per the parsed prospectuses): the block scores "
        "issuer-side dilution only (measured miss, 2026Q2). Read Capital Markets "
        "Activity before trusting it."
    )


def build_report(
    result: AnalysisResult,
    dataset: CompanyDataset,
    *,
    generated_on: str,
    coverage: float | None = None,
    client=None,
    ticker: str | None = None,
    fetched_at: str | None = None,
    fresh: bool = False,
    warnings: list[str] | None = None,
    doc_diagnostics: list[str] | None = None,
    company_facts: dict | None = None,
    submissions: dict | None = None,
    index_degraded: bool = False,
    field_tags: Mapping[str, str | None] | None = None,
    vintage_note: str | None = None,
    field_notes: list[str] | None = None,
    baseline_day: date | None = None,
    vintage_root: Path | None = None,
) -> tuple[str, DistressThermometer]:
    """Assemble the decision card (headline) + full report appendix. Returns
    (markdown, thermometer). Evidence streams are included only when a client is
    provided (CLI/journal); the API passes a dataset alone.

    `generated_on` must be an ISO date (YYYY-MM-DD): it anchors both the card's
    displayed date and the evidence-stream as-of window, so a malformed value is
    rejected up front rather than silently diverging (review finding P3).
    `baseline_day` is the pinned thesis day (journal track): the silent-revision
    check also diffs the newest snapshot against the one at or before it.
    """
    try:
        report_date = date.fromisoformat(generated_on)
    except ValueError as e:
        raise ValueError(
            f"generated_on must be an ISO date (YYYY-MM-DD); got {generated_on!r}"
        ) from e

    # One flag, both surfaces. A caller that had to fall back to per-stream
    # index reads records it here and the appendix warning and the card note
    # follow together — setting one and forgetting the other is what let the
    # first version of this disclosure render a degraded run as clean.
    integrity_notes = [SNAPSHOT_UNAVAILABLE] if index_degraded else []
    warnings = list(warnings or []) + integrity_notes

    body = render(result, generated_on=generated_on)
    event_lines: list[str] = []
    tier1_events: list[str] = []
    errors: dict[str, StreamFailure | None] = {
        "offerings": None, "restatements": None, "events": None, "vintage": None,
    }
    scan = None
    vintage_diff = None

    if client is not None and ticker is not None:
        sections, event_lines, tier1_events, errors, takedowns, scan, vintage_diff = (
            _collect_streams(
                client, ticker, report_date, company_facts, submissions, field_tags,
                baseline_day=baseline_day, vintage_root=vintage_root,
            )
        )
        for section in sections:
            body += "\n\n" + section + "\n"
        caveat = _capital_integrity_offerings_caveat(result, takedowns)
        if caveat is not None:
            event_lines.append(caveat)

    if fetched_at is not None:
        body += "\n\n" + data_quality_section(
            fetched_at=fetched_at,
            fresh=fresh,
            coverage=coverage or 0.0,
            warnings=warnings or [],
            doc_diagnostics=doc_diagnostics or [],
            offerings_error=errors["offerings"],
            restatements_error=errors["restatements"],
            events_error=errors["events"],
            vintage=vintage_note,
            restatement_scan=scan.coverage_line() if scan is not None else None,
            # The client counted its own archive traffic; a client that does
            # not count (a stub) yields no line rather than a guessed one.
            archives=_archive_summary(client),
            field_notes=field_notes,
            vintage_error=errors["vintage"],
            vintage_diff=vintage_diff.status_line() if vintage_diff is not None else None,
        ) + "\n"

    # Tier-1 sources that could NOT be checked this run — restatement footprints
    # and 8-K 4.02 events (offerings is Tier-2 context, not Tier-1). Round-2
    # finding: a not-checked source must not render as checked-and-clean. With no
    # client (API path) none of the evidence streams are checked at all.
    tier1_unavailable: list[str] = []
    if client is None or ticker is None:
        tier1_unavailable = [
            "restatement footprints", "8-K 4.02 events", "silent revisions (vintage diff)",
        ]
    else:
        def _why(name: str) -> str:
            failure = errors[name]
            return " (internal error)" if isinstance(failure, StreamFailure) and failure.internal else ""

        if errors["restatements"] is not None:
            tier1_unavailable.append("restatement footprints" + _why("restatements"))
        if errors["events"] is not None:
            tier1_unavailable.append("8-K 4.02 events" + _why("events"))
        if errors["vintage"] is not None:
            tier1_unavailable.append("silent revisions (vintage diff)" + _why("vintage"))
        elif vintage_diff is not None and not vintage_diff.compared:
            # Two snapshots did not exist yet: not a failure, still not checked.
            tier1_unavailable.append("silent revisions (no vintage baseline yet)")

    # Capital-markets was actually checked iff a client ran offerings without error.
    capital_markets_checked = (
        client is not None and ticker is not None and errors["offerings"] is None
    )

    thermometer = compute_thermometer(result.block_scores, dataset.periods)
    card = render_decision_card(
        result,
        thermometer,
        generated_on=generated_on,
        coverage=coverage,
        event_lines=event_lines or None,
        tier1_events=tier1_events or None,
        tier1_unavailable=tier1_unavailable or None,
        capital_markets_checked=capital_markets_checked,
        integrity_notes=integrity_notes or None,
        # The card must carry what the revision check did not cover: "checked
        # and clean" over a partial inspection is the false clean bill.
        restatement_scan=scan.coverage_line() if scan is not None else None,
        restatement_gaps=len(scan.uninspected) if scan is not None else 0,
    )
    report = (
        card
        + "\n\n"
        + ANALYSIS_SCOPE_NOTICE
        + "\n\n---\n\n# Full report (appendix)\n\n"
        + body
    )
    return report, thermometer


def _pit_dates(dates, since: date, report_date: date) -> list[date]:
    """Event dates inside [since, report_date]. A report dated in the past
    (backtest replay, a journal entry's day) must not carry events filed
    after its own date — the lower cutoff alone let a January 2026 4.02
    appear in a report dated January 2025."""
    return [d for d in sorted(dates) if since <= d <= report_date]


def _pit_footprints(footprints, report_date: date):
    """Deprecated: filtering FINISHED footprints erased amendments that were
    known at report_date but whose figure a later comparative touched again.
    `detect_restatements(as_of=...)` filters the facts instead. Kept only so
    an out-of-tree caller does not break; do not use."""
    return [f for f in footprints if f.current_filed <= report_date]
