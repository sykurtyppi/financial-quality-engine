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

from app.schemas.report import AnalysisResult, Flag
from app.services.scoring.thermometer import DistressThermometer

# Tier-1: validated, low false-positive signals (§7). Mostly event/disclosure
# streams that populate as they are wired (P0-5 restatement, P1-B 8-K events).
TIER1_SIGNALS = frozenset(
    {
        "high_severity_disclosure",
        "restatement_footprint",
        "auditor_change_8k_402",
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
    metrics = set(flag.evidence_metrics)
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
    events_unavailable: bool = False,
) -> str:
    """Render the 90-second card.

    `tier1_events` carries validated event-stream items (8-K Item 4.02
    non-reliance, restatement footprints) that render in Tier-1; high-severity
    disclosure emergence is pulled from the narrative findings automatically.
    `event_lines` carries capital-markets context (offerings) for the events
    section. `events_unavailable` (review finding 4) makes Tier-1 say the event
    stream could not be checked, so a failed fetch never reads as clean.
    """
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
        out += [f"- {c}" for c in result.changes]
    else:
        out.append("- No material period-over-period changes surfaced.")
    out.append("")

    # 2. Distress thermometer
    out += _thermometer_lines(thermometer)
    out.append("")

    # 3. Attention flags — tiered
    out += ["## Attention flags", ""]
    tier_items: dict[int, list[str]] = {1: list(tier1_events or []), 2: [], 3: []}
    if events_unavailable:
        # Review finding 4: a failed event fetch must not read as "clean".
        tier_items[1].append("⚠ event stream unavailable this run — Tier-1 not fully checked (see data quality)")
    # High-severity disclosure emergence is a validated Tier-1 signal (§7).
    for finding in result.narrative_findings:
        if getattr(finding, "kind", "") == "high_severity_disclosure":
            tier_items[1].append(f"High-severity disclosure emergence ({finding.fiscal_label})")
    for f in result.red_flags:
        tier_items[_tier(f)].append(f"{f.title} ({f.fiscal_label})")
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
    else:
        out.append("- No event/capital-markets stream wired into this run.")
    out.append("")

    # 5. Checked and clean
    out += ["## Checked and clean", ""]
    if result.green_flags:
        out += [f"- {f.title} ({f.fiscal_label})" for f in result.green_flags]
    else:
        out.append("- No supportive signals surfaced.")
    out.append("")

    # 6. Data quality
    out += ["## Data quality", ""]
    cov = f"{coverage:.0%}" if coverage is not None else "n/a"
    out.append(f"- XBRL field coverage {cov}; as of {generated_on}.")
    return "\n".join(out)
