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
from datetime import date

from app.schemas.financials import CompanyDataset
from app.schemas.report import AnalysisResult
from app.services.reporting.decision_card import render_decision_card
from app.services.reporting.markdown_report import render
from app.services.scoring.thermometer import DistressThermometer, compute_thermometer


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
    period), not per field — field detail stays in the appendix section."""
    by_event: dict[tuple[str, object, str], list[str]] = defaultdict(list)
    for f in footprints:
        if f.is_amendment:
            by_event[(f.restated_accession, f.period_end, f.restated_form)].append(f.field_name)
    lines = []
    for (_accn, period_end, form), fields in sorted(
        by_event.items(), key=lambda kv: str(kv[0][1]), reverse=True
    ):
        lines.append(
            f"Restatement ({form}) affecting {period_end}: {len(fields)} figure(s) revised "
            "(detail in appendix)"
        )
    return lines


def _collect_streams(client, ticker: str, report_date: date):
    """Fetch offerings, restatements, and 8-K 4.02 events. Returns
    (body_sections, event_lines, tier1_events, errors, unavailable)."""
    body_sections: list[str] = []
    event_lines: list[str] = []
    tier1_events: list[str] = []
    errors = {"offerings": None, "restatements": None, "events": None}
    unavailable: list[str] = []

    try:
        from app.services.ingestion.offerings import fetch_offerings, render_offerings_section

        timeline = fetch_offerings(client, ticker, as_of=report_date)
        body_sections.append(render_offerings_section(timeline))
        if timeline.takedown_count:
            event_lines.append(
                f"{timeline.takedown_count} securities takedown(s) in the last "
                f"{timeline.lookback_months} months (see Capital Markets Activity)"
            )
    except Exception as e:  # noqa: BLE001 - a stream must never break the report
        errors["offerings"] = str(e)
        unavailable.append("capital-markets")

    try:
        from app.services.ingestion.restatements import (
            detect_restatements,
            render_restatements_section,
        )

        cutoff = date(report_date.year - 3, 1, 1)
        footprints = detect_restatements(client.company_facts(ticker), period_since=cutoff)
        body_sections.append(render_restatements_section(footprints))
        tier1_events += _restatement_tier1_lines(footprints)
    except Exception as e:  # noqa: BLE001
        errors["restatements"] = str(e)
        unavailable.append("restatements")

    try:
        from app.services.backtesting.events import fetch_entity_events

        events = fetch_entity_events(client, ticker)
        cutoff = date(report_date.year - 2, report_date.month, min(report_date.day, 28))
        nr_dates = [d for d in sorted(events.non_reliance_8k_dates) if d >= cutoff]
        tier1_events += [
            f"8-K Item 4.02 non-reliance (restatement announced) filed {d}" for d in nr_dates
        ]
    except Exception as e:  # noqa: BLE001
        errors["events"] = str(e)
        unavailable.append("events")

    return body_sections, event_lines, tier1_events, errors, unavailable


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
) -> tuple[str, DistressThermometer]:
    """Assemble the decision card (headline) + full report appendix. Returns
    (markdown, thermometer). Evidence streams are included only when a client is
    provided (CLI/journal); the API passes a dataset alone."""
    body = render(result, generated_on=generated_on)
    event_lines: list[str] = []
    tier1_events: list[str] = []
    unavailable: list[str] = []
    errors = {"offerings": None, "restatements": None, "events": None}

    if client is not None and ticker is not None:
        report_date = date.fromisoformat(generated_on)
        sections, event_lines, tier1_events, errors, unavailable = _collect_streams(
            client, ticker, report_date
        )
        for section in sections:
            body += "\n\n" + section + "\n"

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

    thermometer = compute_thermometer(result.block_scores, dataset.periods)
    card = render_decision_card(
        result,
        thermometer,
        generated_on=generated_on,
        coverage=coverage,
        event_lines=event_lines or None,
        tier1_events=tier1_events or None,
        tier1_unavailable=tier1_unavailable or None,
    )
    report = card + "\n\n---\n\n# Full report (appendix)\n\n" + body
    return report, thermometer
