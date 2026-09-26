"""90-second decision card (P1-D / TARGET_ARCHITECTURE §L11).

The report surface used to lead with the 0-100 composite — the layer measured to
carry near-zero decision value (season review finding 6). This card leads with
the validated readouts instead and carries NO composite grade (§7): the distress
thermometer, tiered attention flags, and the change summary. The full report
follows as an appendix.

Order (§L11): changes -> thermometer -> tiered flags -> events/capital-markets
-> checked-and-clean -> data quality.

Evidence for leading with the thermometer: distressed-control AUC 0.856 vs the
composite's 0.713 (scripts/validate_thermometer.py) and the 2026Q2 season-archive
ablation (docs/thermometer_season_ablation_2026Q2.md).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.schemas.report import AnalysisResult, Flag
from app.services.scoring.thermometer import DistressThermometer

# Tier-1: validated, low false-positive signals (§7). Mostly event/disclosure
# streams that populate as they are wired (P0-5 restatement, P1-B 8-K events).
# Every name in both tiers must be a registered metric or signal kind
# (app/services/metrics_registry.py).
TIER1_SIGNALS = frozenset(
    {
        "high_severity_disclosure",
        "restatement_footprint",
        "auditor_change_8k_401",  # Item 4.01; Item 4.02 is non-reliance
        "non_reliance_8k_402",
        "missed_deadline_nt",
    }
)
# Tier-3: context only — uncalibrated or measured-noisy narrative signals.
TIER3_SIGNALS = frozenset(
    {
        "adjustment_recurrence_ratio",
        "recurring_adjustment_terms",
        "defensive_tone_change",
        "guidance_shift",
        "disclosure_volume_change",
        "kpi_removals",
        "kpi_definition_change",
    }
)


def _tier(flag: Flag) -> int:
    return tier_of(flag.evidence_metrics)


def tier_of(names: Iterable[str]) -> int:
    """The card tier of evidence resting on these metric/signal names: 1
    validated, 2 directional, 3 unvalidated (the evidence ledger labels
    each item the same way the card ranks it)."""
    metrics = set(names)
    if metrics & TIER1_SIGNALS:
        return 1
    if metrics and metrics <= TIER3_SIGNALS:
        return 3
    return 2


def _thermometer_lines(t: DistressThermometer) -> list[str]:
    """Descriptive, no 0-100 number and no band thresholds (review finding 4:
    the thermometer is experimental and uncalibrated; unsupported 45/70 bands
    and a raw score have no basis until reference-class framing exists). Surface
    the concrete facts instead — the regime state and which dimension is most
    elevated."""
    lines = ["## Distress signals (experimental — descriptive, not a score)", ""]
    if t.reading is None:
        lines.append("- Insufficient distress-relevant data this run.")
        return lines
    if t.regime_flags:
        lines.append(
            "- Regime signals present: "
            + "; ".join(f.description for f in t.regime_flags)
            + "."
        )
    else:
        lines.append("- No net-loss / negative-EBITDA regime signal this period.")
    if t.hottest_cluster is not None:
        lines.append(f"- Most-elevated dimension: {t.hottest_cluster.name}.")
    lines.append("")
    lines.append(f"_{t.caveat}_")
    return lines


def render_decision_card(
    result: AnalysisResult,
    thermometer: DistressThermometer,
    *,
    generated_on: str,
    coverage: float | None = None,
    event_lines: list[str] | None = None,
    tier1_events: list[str] | None = None,
    tier1_unavailable: list[str] | None = None,
    capital_markets_checked: bool = False,
    integrity_notes: list[str] | None = None,
    restatement_scan: str | None = None,
    restatement_gaps: int = 0,
    change_notes: dict[str, str] | None = None,
    flag_notes: dict[tuple[str, str], str] | None = None,
) -> str:
    """Render the 90-second card.

    `tier1_events` carries validated event-stream items (8-K Item 4.02
    non-reliance, restatement footprints) that render in Tier-1; high-severity
    disclosure emergence is pulled from the narrative findings automatically.
    `event_lines` carries capital-markets context (offerings) for the events
    section. `tier1_unavailable` names any Tier-1 source that could NOT be
    checked this run (a failed fetch, or streams the API omits) so a
    not-checked source never reads as checked-and-clean (review findings 4 & the
    round-2 Tier-1 availability finding). `integrity_notes` carries acquisition
    guarantees that lapsed this run — the streams were checked, but not
    necessarily against one moment — which belongs on the card for the same
    reason: the 90-second surface must not imply more than the run established.
    `restatement_scan` is the revision check's coverage line and
    `restatement_gaps` the number of fields it could NOT inspect: the
    checked-and-clean header is qualified `(incomplete: ...)` whenever that is
    non-zero, because "clean" over a partial inspection is the false clean
    bill the scan exists to prevent.

    `change_notes` (by change-line label) and `flag_notes` (by flag title and
    fiscal label) mark a line whose metric read a revised figure
    (`revised_inputs.card_notes`): the metric is not wrong, but it is not
    independent of the restatement either, and "clean" resting on a restated
    input must say so.
    """
    change_notes = change_notes or {}
    flag_notes = flag_notes or {}

    def _flagged(f: Flag) -> str:
        line = f"{f.title} ({f.fiscal_label})"
        mark = flag_notes.get((f.title, f.fiscal_label))
        return f"{line} — ⚠ {mark}" if mark else line

    ticker = result.profile.ticker
    out: list[str] = [
        f"# Decision Card — {ticker}",
        "",
        f"_As of {generated_on}. 90-second triage; full report follows as appendix. "
        "The distress signals below are experimental and descriptive, not a "
        "calibrated score._",
        "",
    ]

    # 1. Changes since last period
    out += ["## Changes since last period", ""]
    if result.changes:
        for c in result.changes:
            mark = change_notes.get(c.split(":", 1)[0])
            out.append(f"- {c} — ⚠ {mark}" if mark else f"- {c}")
    else:
        out.append("- No material period-over-period changes surfaced.")
    out.append("")

    # 2. Distress thermometer
    out += _thermometer_lines(thermometer)
    out.append("")

    # 3. Attention flags — tiered
    out += ["## Attention flags", ""]
    tier_items: dict[int, list[str]] = {1: list(tier1_events or []), 2: [], 3: []}
    if tier1_unavailable:
        # A not-checked Tier-1 source must not read as checked-and-clean.
        tier_items[1].append(
            "⚠ not checked this run: " + ", ".join(tier1_unavailable) + " (see data quality)"
        )
    # High-severity disclosure emergence is a validated Tier-1 signal (§7).
    for finding in result.narrative_findings:
        if getattr(finding, "kind", "") == "high_severity_disclosure":
            tier_items[1].append(f"High-severity disclosure emergence ({finding.fiscal_label})")
    for f in result.red_flags:
        tier_items[_tier(f)].append(_flagged(f))
    tier_titles = {
        1: "Tier 1 — validated (low false-positive)",
        2: "Tier 2 — directional (review in context)",
        3: "Tier 3 — context",
    }
    for tier in (1, 2, 3):
        out.append(f"**{tier_titles[tier]}:**")
        items = tier_items[tier]
        out += [f"- {item}" for item in items] if items else ["- none surfaced this run"]
        out.append("")

    # 4. Events & capital markets
    out += ["## Events & capital markets", ""]
    if event_lines:
        out += [f"- {line}" for line in event_lines]
    elif capital_markets_checked:
        # Review finding 2 (round 4): distinguish "checked, none" from "not checked".
        out.append("- Checked — no securities-offering activity in the window.")
    else:
        out.append("- Capital-markets stream not checked this run (see data quality).")
    out.append("")

    # 5. Checked and clean — qualified whenever the revision check had holes.
    header = "## Checked and clean"
    if restatement_gaps:
        header += (
            f" (incomplete: {restatement_gaps} field(s) not inspectable for "
            "revisions — see data quality)"
        )
    out += [header, ""]
    if result.green_flags:
        out += [f"- {_flagged(f)}" for f in result.green_flags]
    else:
        out.append("- No supportive signals surfaced.")
    out.append("")

    # 6. Data quality
    out += ["## Data quality", ""]
    if coverage is not None:
        out.append(f"- XBRL field coverage {coverage:.0%}; as of {generated_on}.")
    else:
        # Dataset-only mode (e.g. the API): no EDGAR acquisition, so coverage and
        # the evidence streams were not measured this run.
        out.append(
            f"- Dataset-only run (as of {generated_on}): coverage and EDGAR "
            "evidence streams not measured — run via generate_report.py for full detail."
        )
    for note in integrity_notes or []:
        out.append(f"- ⚠ {note}")
    if restatement_scan is not None:
        out.append(f"- Restatement scan: {restatement_scan}.")
    return "\n".join(out)
