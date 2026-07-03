"""Scoring configuration: blocks, weights, and concern-mapping anchors.

STATUS: v0.3 — PARTIALLY CALIBRATED. Block weights and two component
exclusions were adjusted from the 2021-2025 walk-forward backtest
(docs/calibration_report.md): ~70 companies, point-in-time fundamentals,
survivorship-biased universe. Anchor points remain judgment-based heuristics.
This is directional evidence, NOT a statistically validated model; the engine
still attaches an uncalibrated-thresholds caveat to every output.

Evidence summary driving the v0.3 weight changes:
- Capex Discipline: right-signed vs returns, margins, AND forward FCF -> up.
- Earnings Quality (accrual family): strongest predictor of forward operating-
  margin deterioration (IC ~ -0.20 to -0.25) -> up.
- Cash Conversion & Balance Sheet: right-signed vs 12M relative returns
  (leverage components strongest; partly a 2022 rate-cycle effect) -> up.
- Capital Integrity (SBC/dilution family): WRONG-SIGNED vs forward
  fundamentals in this growth-heavy universe (SBC intensity coincided with
  improving margins) -> down; kept as descriptive, not predictive.
- Working Capital: near-zero signal; wc_swing wrong-signed -> down.
- buyback_offset_ratio and working_capital_swing_to_income: excluded from
  scoring (weight 0, still computed and reported).
- Narrative Drift: NOT testable in the backtest (no historical documents);
  weight kept moderate, fully uncalibrated.

Anchor format: a list of (metric_value, concern_score) points, ascending by
metric value. The engine interpolates linearly between anchors and clamps
outside the range. Concern scores are 0-100 (0 = no concern, 100 = maximum
concern) — direction is therefore encoded in the anchors themselves.

Sector normalization hook: SECTOR_ANCHOR_OVERRIDES maps sector name ->
{metric_name: anchors} and takes precedence when the company's sector matches.
Empty in v0 by design — populating it without evidence would be false precision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

V0_HEURISTIC = True  # anchors are still heuristic; weights partially calibrated
CONFIG_VERSION = "0.3.0"

# Direction bands, set from the empirical backtest score distribution
# (n=1141 point-in-time scores: p50=31.7, p90=45.1). The original 35/60 bands
# assumed the full 0-100 range is used; in practice scores compress to ~17-67.
DIRECTION_POSITIVE_BELOW = 32.0
DIRECTION_NEGATIVE_ABOVE = 45.0

Anchors = list[tuple[float, float]]


@dataclass(frozen=True)
class MetricSpec:
    metric_name: str
    weight: float
    anchors: Anchors


@dataclass(frozen=True)
class BlockSpec:
    name: str
    metrics: list[MetricSpec] = field(default_factory=list)


BLOCKS: list[BlockSpec] = [
    BlockSpec(
        name="Earnings Quality",
        metrics=[
            MetricSpec("total_accruals", 0.30, [(-0.05, 10), (0.0, 25), (0.05, 55), (0.10, 80), (0.15, 95)]),
            MetricSpec("accrual_trend", 0.25, [(-0.02, 15), (0.0, 30), (0.03, 60), (0.08, 85)]),
            MetricSpec("beneish_m_score", 0.30, [(-3.0, 10), (-2.22, 35), (-1.78, 65), (-1.0, 85)]),
            MetricSpec("beneish_tata", 0.15, [(-0.05, 12), (0.0, 28), (0.05, 58), (0.12, 88)]),
        ],
    ),
    BlockSpec(
        name="Revenue Quality",
        metrics=[
            MetricSpec("receivables_growth_spread", 0.35, [(0.0, 20), (0.10, 45), (0.25, 70), (0.50, 90)]),
            MetricSpec("beneish_dsri", 0.25, [(1.0, 20), (1.15, 45), (1.40, 70), (1.80, 90)]),
            MetricSpec("deferred_revenue_growth_spread", 0.20, [(-0.40, 85), (-0.15, 60), (0.0, 35), (0.10, 20)]),
            MetricSpec("dso_trend", 0.20, [(0.0, 25), (5.0, 50), (15.0, 75), (30.0, 90)]),
        ],
    ),
    BlockSpec(
        name="Cash Conversion",
        metrics=[
            MetricSpec("cfo_to_net_income", 0.35, [(0.0, 95), (0.4, 85), (0.7, 65), (0.9, 45), (1.1, 20), (1.3, 10)]),
            MetricSpec("fcf_to_net_income", 0.25, [(0.0, 90), (0.3, 75), (0.6, 55), (0.9, 30), (1.1, 10)]),
            MetricSpec("fcf_margin", 0.20, [(-0.10, 80), (0.0, 60), (0.05, 45), (0.12, 25), (0.20, 10)]),
            MetricSpec("fcf_margin_trend", 0.20, [(-0.08, 85), (-0.03, 60), (0.0, 35), (0.03, 20)]),
        ],
    ),
    BlockSpec(
        name="Working Capital Stress",
        metrics=[
            MetricSpec("inventory_growth_spread", 0.60, [(0.0, 20), (0.10, 45), (0.30, 70), (0.60, 90)]),
            MetricSpec("dio_trend", 0.40, [(0.0, 25), (5.0, 45), (15.0, 70), (30.0, 90)]),
            # Excluded from scoring in v0.3: wrong-signed vs forward margins and
            # FCF in the backtest (IC +0.06..+0.15). Still computed and reported.
            MetricSpec("working_capital_swing_to_income", 0.0, [(0.2, 15), (0.5, 35), (1.0, 60), (2.0, 85)]),
        ],
    ),
    BlockSpec(
        name="Capital Integrity",
        metrics=[
            MetricSpec("sbc_to_revenue", 0.25, [(0.02, 10), (0.05, 25), (0.10, 50), (0.20, 75), (0.30, 90)]),
            MetricSpec("sbc_to_cfo", 0.20, [(0.1, 10), (0.3, 30), (0.6, 55), (1.0, 80), (1.5, 92)]),
            MetricSpec("diluted_share_growth", 0.15, [(0.0, 20), (0.02, 35), (0.05, 60), (0.10, 85)]),
            MetricSpec("net_share_count_change", 0.15, [(-0.02, 15), (0.0, 30), (0.03, 55), (0.08, 80)]),
            # Excluded from scoring in v0.3: wrong-signed in the backtest — the
            # anchors punished non-buyback companies. Still computed and reported.
            MetricSpec("buyback_offset_ratio", 0.0, [(0.0, 70), (0.5, 55), (1.0, 40), (2.0, 20)]),
            # Right-signed across all three outcome families -> weight up.
            MetricSpec("issuance_pressure", 0.25, [(0.0, 15), (0.2, 40), (0.5, 65), (1.0, 85)]),
        ],
    ),
    BlockSpec(
        name="Capex Discipline",
        metrics=[
            MetricSpec("capex_growth_spread", 0.30, [(0.0, 25), (0.30, 50), (0.80, 75), (1.50, 90)]),
            MetricSpec("capex_to_da", 0.20, [(1.0, 20), (1.5, 35), (2.5, 60), (4.0, 80)]),
            MetricSpec("capex_intensity_regime_shift", 0.25, [(0.0, 25), (0.03, 45), (0.08, 70), (0.15, 88)]),
            MetricSpec("incremental_revenue_per_capex", 0.25, [(0.0, 85), (0.3, 65), (0.8, 45), (1.5, 25), (3.0, 10)]),
        ],
    ),
    BlockSpec(
        name="Balance Sheet Stress",
        metrics=[
            MetricSpec("net_debt_to_ebitda", 0.20, [(0.0, 10), (1.0, 25), (2.5, 45), (4.0, 70), (6.0, 90)]),
            MetricSpec("interest_coverage", 0.20, [(1.0, 90), (2.0, 75), (4.0, 55), (8.0, 30), (15.0, 12)]),
            MetricSpec("current_ratio", 0.15, [(0.5, 85), (0.8, 70), (1.0, 55), (1.5, 30), (2.5, 15)]),
            MetricSpec("debt_to_assets", 0.10, [(0.1, 15), (0.3, 35), (0.5, 60), (0.7, 82)]),
            MetricSpec("leverage_change", 0.10, [(-0.02, 20), (0.0, 30), (0.05, 60), (0.12, 85)]),
            MetricSpec("asset_quality_proxy", 0.15, [(0.2, 20), (0.4, 40), (0.6, 65), (0.8, 85)]),
            MetricSpec("intangibles_to_assets", 0.10, [(0.1, 15), (0.3, 40), (0.5, 65), (0.7, 85)]),
        ],
    ),
    BlockSpec(
        name="Narrative Drift",
        metrics=[
            MetricSpec("adjustment_recurrence_ratio", 0.30, [(0.0, 10), (0.3, 30), (0.6, 55), (0.9, 80), (1.0, 88)]),
            MetricSpec("recurring_adjustment_terms", 0.25, [(0.0, 15), (1.0, 35), (3.0, 60), (6.0, 85)]),
            MetricSpec("kpi_removals", 0.25, [(0.0, 15), (1.0, 45), (2.0, 65), (4.0, 88)]),
            MetricSpec("disclosure_volume_change", 0.20, [(0.5, 80), (0.75, 60), (0.9, 40), (1.0, 28), (1.2, 15)]),
        ],
    ),
]

# Overall Quality Risk Score block weights. Sum to 1.0.
# v0.3: adjusted from backtest evidence (see module docstring and
# docs/calibration_report.md). v0 values in comments for provenance.
BLOCK_WEIGHTS: dict[str, float] = {
    "Earnings Quality": 0.20,       # v0: 0.18 — margin-deterioration signal confirmed
    "Cash Conversion": 0.17,        # v0: 0.15 — 12M return signal confirmed
    "Revenue Quality": 0.10,        # v0: 0.13 — near-zero measured signal
    "Working Capital Stress": 0.07, # v0: 0.10 — weak/wrong-signed components
    "Capital Integrity": 0.07,      # v0: 0.12 — wrong-signed vs fundamentals; descriptive
    "Capex Discipline": 0.15,       # v0: 0.10 — right-signed vs all outcome families
    "Balance Sheet Stress": 0.14,   # v0: 0.10 — strongest return components (rate-cycle caveat)
    "Narrative Drift": 0.10,        # v0: 0.12 — untestable in backtest; fully uncalibrated
}

# Sector normalization hook (see module docstring). Intentionally empty in v0.
SECTOR_ANCHOR_OVERRIDES: dict[str, dict[str, Anchors]] = {}

# Revenue growth (SGI) above this triggers the high-growth-profile caveat on
# growth-sensitive blocks to reduce false positives on scale-up companies.
HIGH_GROWTH_SGI_THRESHOLD = 1.4
GROWTH_SENSITIVE_BLOCKS = {"Revenue Quality", "Working Capital Stress", "Capex Discipline", "Capital Integrity"}
HIGH_GROWTH_CAVEAT = (
    "High-growth profile detected (revenue growth above 40% period-over-period): "
    "working-capital build, capex acceleration, and SBC intensity may reflect "
    "scale-up dynamics rather than quality deterioration. Requires analyst review "
    "against unit economics before drawing conclusions."
)

V0_WEIGHTS_CAVEAT = (
    "Scores use v0.3 weights informed by a small, survivorship-biased 2021-2025 "
    "backtest (~70 companies, point-in-time fundamentals); anchor thresholds "
    "remain judgment-based heuristics and are not sector-normalized. Directional "
    "evidence only — treat as a screening aid, not a calibrated probability. "
    "Methodology and limits: docs/calibration_report.md."
)
