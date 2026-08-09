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

Not yet included (evidence-gated, later increments):
- Own-history and same-year percentile framing needs the L5 reference-class
  store (roadmap P2-E). Until then the reading is built from the existing
  concern anchors and is HEURISTIC — labelled as such, like the block scores.
- The equity<0 (OENEG) regime dummy needs a mapped stockholders-equity field
  the ingestion layer does not yet produce; tracked as a follow-up.

The thermometer is additive: it does not remove or alter the composite. Removal
is gated on a validation harness showing it discriminates better than the
composite on the season archive + distressed controls (roadmap P1-C kill gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.financials import PeriodFinancials
from app.schemas.scoring import BlockScore

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
    block_scores: list[BlockScore], periods: list[PeriodFinancials]
) -> DistressThermometer:
    """AOM over distress clusters + additive regime dummies -> a single 0-100
    distress reading. Returns reading=None only when neither a distress cluster
    nor a regime dummy is computable."""
    concern = _concern_by_metric(block_scores)

    clusters: list[ClusterReadout] = []
    for name, members in DISTRESS_CLUSTERS.items():
        present = [m for m in members if m in concern]
        if not present:
            continue
        mean = sum(concern[m] for m in present) / len(present)
        clusters.append(ClusterReadout(name=name, concern=round(mean, 1), members=tuple(present)))

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
