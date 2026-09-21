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

from collections import defaultdict
from collections.abc import Mapping
from datetime import date

from app.schemas.financials import CompanyDataset
from app.schemas.report import AnalysisResult
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
    offerings_error: str | None = None,
    restatements_error: str | None = None,
    events_error: str | None = None,
) -> str:
    """A fetch failure must be distinguishable from 'the filer didn't disclose'
    (P0-D), for every stream including events (review finding 4)."""
    lines = [
        "## Appendix: Data Acquisition Quality",
        "",
        f"- Data fetched: {fetched_at} "
        + ("(caches bypassed)" if fresh else "(EDGAR JSON caches up to 24h old; use --fresh on filing days)"),
        f"- XBRL field coverage: {coverage:.0%}",
    ]
    lines += [f"- Ingestion warning: {w}" for w in warnings]
    lines += [f"- Document acquisition: {d}" for d in doc_diagnostics]
    for label, err, gap in (
        ("Capital-markets", offerings_error, "no activity"),
        ("Restatement", restatements_error, "no revisions"),
        ("Event (8-K 4.02)", events_error, "no events"),
    ):
        if err is not None:
            lines.append(
                f"- **{label} appendix UNAVAILABLE** (fetch/parse failed: {err}). "
                f"Absence of that section is a data gap, not evidence of {gap}."
            )
    return "\n".join(lines)


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


# Exception types that are almost always a defect in this code rather than a
# problem with what SEC returned. A broad `except Exception` around each
# evidence stream is deliberate — one failing stream must never cost the whole
# report — but it was also labelling every failure a fetch/parse problem, so a
# misplaced variable surfaced to the reader as:
#
#   Restatement appendix UNAVAILABLE (fetch/parse failed: name 'field_tags'
#   is not defined). Absence of that section is a data gap, not evidence of
#   no revisions.
#
# which blames SEC for a bug and invites the reader to discount a section that
# was never computed. These re-raise instead: a defect should fail loudly in
# CI and in an operator's run, where it gets fixed, rather than hide behind a
# data-gap notice for as long as nobody reads the code.
#
# The trade-off is deliberate. An unforeseen SEC payload shape that raises one
# of these will now break the report instead of degrading it — noisy, but
# recoverable and visible, where the alternative is a wrong all-clear that
# nobody investigates.
_PROGRAMMING_ERRORS = (
    NameError, AttributeError, TypeError, ImportError, AssertionError, IndexError,
)


def _stream_failure(exc: Exception) -> str:
    """Record an acquisition failure, or re-raise a defect."""
    if isinstance(exc, _PROGRAMMING_ERRORS):
        raise exc
    return str(exc)


def _collect_streams(
    client,
    ticker: str,
    report_date: date,
    company_facts: dict | None = None,
    submissions: dict | None = None,
    field_tags: Mapping[str, str | None] | None = None,
):
    """Fetch offerings, restatements, and 8-K 4.02 events. Returns
    (body_sections, event_lines, tier1_events, errors, takedowns). `takedowns`
    is the CLASSIFIED OfferingFiling list (not a count): the Capital Integrity
    caveat must attribute only what the parsed records establish — a debt
    424B5 or an issuer-primary deal is not a sponsor sale. Stream
    availability is derived from `errors` by the caller — there is no separate
    list."""
    body_sections: list[str] = []
    event_lines: list[str] = []
    tier1_events: list[str] = []
    errors = {"offerings": None, "restatements": None, "events": None}
    takedowns: list = []

    try:
        from app.services.ingestion.offerings import fetch_offerings, render_offerings_section

        timeline = fetch_offerings(client, ticker, as_of=report_date, submissions=submissions)
        body_sections.append(render_offerings_section(timeline))
        # Review finding 1 (round 5): fetch_offerings swallows a submissions
        # outage into a structured acquisition_error instead of raising, so check
        # it explicitly — otherwise an outage reads as checked-and-clean.
        if timeline.acquisition_error is not None:
            errors["offerings"] = timeline.acquisition_error
        elif timeline.takedown_count:
            takedowns = list(timeline.takedowns)
            event_lines.append(
                f"{timeline.takedown_count} securities takedown(s) in the last "
                f"{timeline.lookback_months} months (see Capital Markets Activity)"
            )
    except Exception as e:  # noqa: BLE001 - a stream must never break the report
        errors["offerings"] = _stream_failure(e)

    try:
        from app.services.ingestion.restatements import (
            detect_restatements,
            render_restatements_section,
        )

        cutoff = date(report_date.year - 3, 1, 1)
        facts = company_facts if company_facts is not None else client.company_facts(ticker)
        footprints = detect_restatements(
            facts, period_since=cutoff, as_of=report_date, selected_tags=field_tags
        )
        body_sections.append(render_restatements_section(footprints))
        tier1_events += _restatement_tier1_lines(footprints)
    except Exception as e:  # noqa: BLE001
        errors["restatements"] = _stream_failure(e)

    try:
        from app.services.backtesting.events import fetch_entity_events

        events = fetch_entity_events(client, ticker, submissions=submissions)
        cutoff = date(report_date.year - 2, report_date.month, min(report_date.day, 28))
        nr_dates = _pit_dates(events.non_reliance_8k_dates, cutoff, report_date)
        tier1_events += [
            f"8-K Item 4.02 non-reliance (restatement announced) filed {d}" for d in nr_dates
        ]
    except Exception as e:  # noqa: BLE001
        errors["events"] = _stream_failure(e)

    return body_sections, event_lines, tier1_events, errors, takedowns


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
) -> tuple[str, DistressThermometer]:
    """Assemble the decision card (headline) + full report appendix. Returns
    (markdown, thermometer). Evidence streams are included only when a client is
    provided (CLI/journal); the API passes a dataset alone.

    `generated_on` must be an ISO date (YYYY-MM-DD): it anchors both the card's
    displayed date and the evidence-stream as-of window, so a malformed value is
    rejected up front rather than silently diverging (review finding P3).
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
    errors = {"offerings": None, "restatements": None, "events": None}

    if client is not None and ticker is not None:
        sections, event_lines, tier1_events, errors, takedowns = _collect_streams(
            client, ticker, report_date, company_facts, submissions, field_tags
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
        ) + "\n"

    # Tier-1 sources that could NOT be checked this run — restatement footprints
    # and 8-K 4.02 events (offerings is Tier-2 context, not Tier-1). Round-2
    # finding: a not-checked source must not render as checked-and-clean. With no
    # client (API path) none of the evidence streams are checked at all.
    tier1_unavailable: list[str] = []
    if client is None or ticker is None:
        tier1_unavailable = ["restatement footprints", "8-K 4.02 events"]
    else:
        if errors["restatements"] is not None:
            tier1_unavailable.append("restatement footprints")
        if errors["events"] is not None:
            tier1_unavailable.append("8-K 4.02 events")

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
