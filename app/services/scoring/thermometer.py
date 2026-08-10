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
   missing data. This is the principled, thermometer-level form of the P0-9 fix.
   Invariant: a distress metric NEVER drops out and lets weight renormalize to
   less-alarming survivors — it is scored at its max anchor and kept at full
   weight (engine.py), and regime dummies add on top. (Within a single cluster's
   average, a max-anchor-capped distress metric can shift the mean by a fraction
   of a point vs the non-distress subset when that subset already averages above
   the cap; the no-renormalization guarantee is at the weight/regime level, not
   an exact per-cluster-mean monotonicity claim.)

Own-history percentile framing (`history=` arg) is also implemented but is NOT
the default — see the finding below.

EMPIRICAL FINDING (kill gate — measured offline on real cached companyfacts,
healthy vs stressed cohorts):
- Early 4-cluster anchor reading: separation +5.2 vs composite +1.8, but
  max-across-clusters amplified the single-metric Liquidity cluster
  (current_ratio), so cash-efficient staples (PG 73) read falsely hot.
- Own-history-percentile reading: separation −7.0 (WORSE). Own history measures
  DETERIORATION, not absolute level; with ~8 quarters every company has some
  cluster at its own recent extreme, which max amplifies, and a stably-
  distressed firm reads low because bad is normal for it. Absolute level needs
  cross-sectional peer percentiles — the L5 reference-class store (P2-E).
- DESIGN CHANGE: two well-populated correlated clusters instead of one per
  metric, so current_ratio is averaged with leverage and a staple's low-but-
  normal ratio no longer becomes the max-across-clusters headline. Anchor-based,
  no anchor changes (composite/golden untouched). On hand-picked cohorts this
  looked strong (separation +15 vs +1, healthy ceiling 47 vs 73-90).

STATUS: EXPERIMENTAL — NOT a validated replacement for the composite (review
findings 1-3). scripts/validate_thermometer.py reports indicative, in-sample AUC
on the distressed-control cohort (regime-inclusive: company-level 0.99 vs the
composite's 0.95; company-quarter 0.856 vs 0.713). Every caveat matters:
  - Tiny n: 6 stress companies (BYND, CVNA, LCID, PTON, SMCI, W) vs 25 controls.
    The 95/425 "observations" are pseudo-replicated company-quarters; AUC on them
    understates uncertainty, so company-level AUC is primary and at n=6 the gap
    is within noise.
  - In-sample and ex-post: the cohort is qualitative evidence ("not proof", per
    universe.py), and the clustering/config were chosen after seeing the data.
  - Not reproducible from a clean checkout: the regime dummies need the
    git-ignored data/cache/*.json; only the cluster-only figure (which ties the
    composite) is reproducible from the committed CSV.
The regime dummies do carry the signal (Ohlson: the composite renormalizes
NI<0/EBITDA<0 away — the P0-9 inversion — the thermometer ADDS them), but the
above is NOT enough to retire the composite. It stays until out-of-sample and
reference-class (P2-E) validation exist. Treat this module as experimental.

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

# Minimum present members for a cluster to contribute (review finding 3). Under
# max-across-clusters a lone available metric would otherwise become the whole
# reading (e.g. current_ratio alone -> 90/100 at ~10% coverage). A cluster with
# fewer than this many present members is treated as insufficient, not scored.
MIN_CLUSTER_MEMBERS = 2

THERMOMETER_HEURISTIC_CAVEAT = (
    "EXPERIMENTAL distress reading — not validated and not a calibrated "
    "probability of failure. Built from uncalibrated concern anchors and regime "
    "dummies; only tested in-sample on a tiny cohort. Reference-class percentile "
    "framing (own-history and same-year peers) is pending."
)

# Correlated clusters for AOM aggregation. Two well-populated clusters, NOT one
# per metric: under max-across-clusters a single-metric "cluster" becomes the
# headline whenever that one metric is noisy or miscalibrated (an early split
# let current_ratio make cash-efficient staples read falsely hot). Grouping
# correlated balance-sheet signals so they average together neutralises that —
# a staple's low-but-normal current ratio is averaged down by its healthy
# leverage. Membership is by metric name; absent metrics are skipped.
DISTRESS_CLUSTERS: dict[str, tuple[str, ...]] = {
    "Balance Sheet & Leverage": (
        "net_debt_to_ebitda",
        "interest_coverage",
        "current_ratio",
        "debt_to_assets",
        "leverage_change",
    ),
    "Cash Generation & Funding": (
        "cfo_to_net_income",
        "fcf_margin",
        "fcf_margin_trend",
        "issuance_pressure",
    ),
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
) -> tuple[list[float], list[str]]:
    """Concern input per member that actually contributes: own-history
    percentile when enough history exists, else the absolute anchor concern.
    Returns (values, contributing_member_names) — kept in sync so the readout
    cannot list a member that added no value."""
    inputs: list[float] = []
    contributed: list[str] = []
    for m in members:
        if history is not None and m in history:
            vals = [r.value for r in history[m] if r.status is MetricStatus.OK and r.value is not None]
            if len(vals) >= MIN_HISTORY_FOR_PERCENTILE:
                inputs.append(_own_history_concern(vals, directions.get(m, 1.0)))
                contributed.append(m)
                continue
        if m in concern:
            inputs.append(concern[m])
            contributed.append(m)
    return inputs, contributed


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
        # `contributed` are the members that actually produced a value, so the
        # readout never lists a phantom member that added nothing to the mean.
        inputs, contributed = _cluster_inputs(members, concern, history, directions)
        if len(inputs) < MIN_CLUSTER_MEMBERS:
            continue  # a single available metric is not a reliable cluster
        mean = sum(inputs) / len(inputs)
        clusters.append(ClusterReadout(name=name, concern=round(mean, 1), members=tuple(contributed)))

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
