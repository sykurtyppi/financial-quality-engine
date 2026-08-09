"""Distress thermometer — the aggregation that replaces the composite (P1-C).

The 0-100 composite averages ~43 subscores through two stages and measured
non-discriminating (11 live runs all 31-58 "Mixed"). Gap research Gap 1 is
blunt: no validated forensic/distress score aggregates by weighted-averaging
dozens of subscores — the ESG-composite literature shows that design destroys
discrimination regardless of weights.

This module implements the replacement aggregation from TARGET_ARCHITECTURE §7,
for the DISTRESS plane only (the engine's one validated capability — 75-83%
eventual-failure capture at p90):

1. AOM aggregation (Aggarwal, SIGKDD 2013): **average within correlated
   clusters, max across clusters.** Plain averaging buries the one screaming
   detector; plain max is unstable; average-within/max-across beats both. The
   distress-relevant concern scores are grouped into correlated clusters
   (leverage, liquidity, cash generation, capital dependence); each cluster
   contributes its mean, and the thermometer takes the MAX across clusters.

2. Ohlson-style regime dummies (O-score, 45 years) that ADD concern instead of
   renormalizing away. NI<0, NI<0 in both recent quarters (INTWO), EBITDA<0 are
   first-class distress predictors — a broken ratio regime is signal, not
   missing data. This is the principled, thermometer-level form of the P0-9 fix:
   distress states raise the reading, they can never lower it.

Own-history percentile framing (`history=` arg) is implemented: each ratio is
ranked within the company's own history, oriented by the concern direction.

EMPIRICAL FINDING (kill gate — recorded honestly, this is why the composite
STAYS for now). Measured offline on real cached companyfacts, healthy vs
stressed cohorts:
- Anchor-based reading: separation +5.2 vs composite +1.8, BUT max-across-
  clusters amplifies the single-metric Liquidity cluster (current_ratio), so
  cash-efficient staples (PG 73) read falsely hot — an ABSOLUTE-level
  miscalibration.
- Own-history-percentile reading: separation −7.0 (WORSE). Own history measures
  DETERIORATION, not absolute level; with ~8 quarters, every company has some
  cluster at its own recent extreme, which max amplifies, and a stably-
  distressed firm reads low because bad is normal for it.
Neither passes. The two failures are complementary and confirm §7's spec, which
calls for own-history AND same-year percentiles: the cross-sectional (peer)
percentile supplies the absolute level own-history lacks. That needs the L5
reference-class store (roadmap P2-E), which does not exist yet.

=> The distress thermometer is BLOCKED on P2-E. This module is the aggregation
machinery (AOM + regime dummies + percentile framing), correct and reusable —
the same `_own_history_concern` logic applies to peer distributions once P2-E
supplies them. It is additive and wired into no surface; the composite is NOT
removed until a reference-class-backed reading beats it on the kill test.

Also not yet included: the equity<0 (OENEG) regime dummy needs a mapped
stockholders-equity field the ingestion layer does not yet produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import scoring_config as cfg
from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.scoring import BlockScore

# Minimum OK observations before a ratio is judged against its own history
# rather than the absolute anchor. Below this a percentile is meaningless.
MIN_HISTORY_FOR_PERCENTILE = 5

THERMOMETER_HEURISTIC_CAVEAT = (
    "Distress thermometer is a heuristic reading built from uncalibrated concern "
    "anchors and regime dummies; it is not a calibrated probability of failure. "
    "Percentile framing against own history and same-year peers is pending the "
    "reference-class store."
)

# Correlated clusters for AOM aggregation. Membership is by metric name; metrics
# absent from the scored output are simply skipped, so listing a not-yet-scored
# candidate is harmless.
DISTRESS_CLUSTERS: dict[str, tuple[str, ...]] = {
    "Leverage & Coverage": (
        "net_debt_to_ebitda",
        "interest_coverage",
        "debt_to_assets",
        "leverage_change",
    ),
    "Liquidity": ("current_ratio",),
    "Cash Generation": ("cfo_to_net_income", "fcf_margin", "fcf_margin_trend"),
    "Capital Dependence": ("issuance_pressure",),
}


@dataclass(frozen=True)
class ClusterReadout:
    name: str
    concern: float  # mean of member concern scores (0-100)
    members: tuple[str, ...]  # metric names that contributed


@dataclass(frozen=True)
class RegimeFlag:
    code: str
    description: str
    concern_add: float  # additive concern contribution


@dataclass
class DistressThermometer:
    reading: float | None  # 0-100, or None when nothing is computable
    clusters: list[ClusterReadout] = field(default_factory=list)
    regime_flags: list[RegimeFlag] = field(default_factory=list)
    caveat: str = THERMOMETER_HEURISTIC_CAVEAT

    @property
    def hottest_cluster(self) -> ClusterReadout | None:
        return max(self.clusters, key=lambda c: c.concern) if self.clusters else None


def _concern_by_metric(block_scores: list[BlockScore]) -> dict[str, float]:
    out: dict[str, float] = {}
    for bs in block_scores:
        for c in bs.components:
            if c.concern_score is not None:
                out[c.metric_name] = c.concern_score
    return out


def _metric_directions() -> dict[str, float]:
    """+1 if a higher metric value means more concern, -1 if lower means more,
    derived from the concern-anchor slope in the config."""
    out: dict[str, float] = {}
    for block in cfg.BLOCKS:
        for ms in block.metrics:
            pts = sorted(ms.anchors, key=lambda p: p[0])
            out[ms.metric_name] = 1.0 if pts[-1][1] >= pts[0][1] else -1.0
    return out


def _own_history_concern(values: list[float], direction: float) -> float:
    """Percentile of the CURRENT value (last element) within the company's own
    history, oriented so that 'more concerning than typical' scores high. A
    structurally-low-but-stable ratio (P&G's current ratio) sits near its own
    median -> ~50, not the false-high the absolute anchor produces; a genuine
    deterioration to a new extreme scores near 100."""
    current = values[-1]
    n = len(values)
    if direction >= 0:  # higher value = worse
        less_bad = sum(1 for v in values if v < current)
    else:  # lower value = worse
        less_bad = sum(1 for v in values if v > current)
    equal = sum(1 for v in values if v == current)
    return round((less_bad + 0.5 * equal) / n * 100.0, 1)


def _cluster_inputs(
    members: tuple[str, ...],
    concern: dict[str, float],
    history: dict[str, list[MetricResult]] | None,
    directions: dict[str, float],
) -> tuple[list[float], bool]:
    """Concern input per present member: own-history percentile when enough
    history exists, else the absolute anchor concern. Returns (values,
    used_percentile_for_any)."""
    inputs: list[float] = []
    used_percentile = False
    for m in members:
        if history is not None and m in history:
            vals = [r.value for r in history[m] if r.status is MetricStatus.OK and r.value is not None]
            if len(vals) >= MIN_HISTORY_FOR_PERCENTILE:
                inputs.append(_own_history_concern(vals, directions.get(m, 1.0)))
                used_percentile = True
                continue
        if m in concern:
            inputs.append(concern[m])
    return inputs, used_percentile


def _regime_flags(periods: list[PeriodFinancials]) -> list[RegimeFlag]:
    """Ohlson-style dummies from the raw financials. NI<0 in both recent
    quarters supersedes the single-quarter flag (no double count)."""
    if not periods:
        return []
    ordered = sorted(periods, key=lambda p: p.period_end)
    cur = ordered[-1]
    prev = ordered[-2] if len(ordered) >= 2 else None
    flags: list[RegimeFlag] = []

    ebitda = cur.ebitda
    if ebitda is not None and ebitda < 0:
        flags.append(RegimeFlag("EBITDA_NEGATIVE", "EBITDA is negative", 25.0))

    ni = cur.net_income
    prev_ni = prev.net_income if prev is not None else None
    if ni is not None and ni < 0:
        if prev_ni is not None and prev_ni < 0:
            flags.append(
                RegimeFlag("NI_NEGATIVE_2Q", "Net loss in the two most recent quarters", 20.0)
            )
        else:
            flags.append(RegimeFlag("NI_NEGATIVE", "Net loss this quarter", 10.0))
    return flags


def compute_thermometer(
    block_scores: list[BlockScore],
    periods: list[PeriodFinancials],
    history: dict[str, list[MetricResult]] | None = None,
) -> DistressThermometer:
    """AOM over distress clusters + additive regime dummies -> a single 0-100
    distress reading. Returns reading=None only when neither a distress cluster
    nor a regime dummy is computable.

    When `history` is supplied, each ratio is judged against its own history
    (percentile framing) rather than the absolute anchor — the fix for
    structurally-low-but-stable ratios reading falsely hot. Without history it
    falls back to the anchor concern scores.
    """
    concern = _concern_by_metric(block_scores)
    directions = _metric_directions()

    clusters: list[ClusterReadout] = []
    for name, members in DISTRESS_CLUSTERS.items():
        present = tuple(m for m in members if m in concern or (history is not None and m in history))
        inputs, _ = _cluster_inputs(members, concern, history, directions)
        if not inputs:
            continue
        mean = sum(inputs) / len(inputs)
        clusters.append(ClusterReadout(name=name, concern=round(mean, 1), members=present))

    regime_flags = _regime_flags(periods)

    if not clusters and not regime_flags:
        return DistressThermometer(reading=None, clusters=[], regime_flags=regime_flags)

    # AOM: max across clusters. Regime dummies ADD on top (bounded at 100).
    base = max((c.concern for c in clusters), default=0.0)
    regime_add = sum(f.concern_add for f in regime_flags)
    reading = min(100.0, base + regime_add)

    return DistressThermometer(
        reading=round(reading, 1),
        clusters=sorted(clusters, key=lambda c: c.concern, reverse=True),
        regime_flags=regime_flags,
    )
