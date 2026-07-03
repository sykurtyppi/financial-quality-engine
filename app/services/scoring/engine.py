"""Scoring engine: maps computed metrics to block concern scores and an
overall score, with full component transparency.

Conventions (see docs/scoring_methodology.md):
- All scores are 0-100 CONCERN scores (0 = no concern).
- A block with insufficient usable metrics returns score=None, never a
  fabricated midpoint.
- Weights renormalize over available metrics; data coverage and confidence
  are reported so renormalization is never invisible.
"""

from __future__ import annotations

from app.config import scoring_config as cfg
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.scoring import (
    BlockScore,
    ComponentContribution,
    Confidence,
    Direction,
    OverallScore,
)

MIN_COVERAGE_FOR_SCORE = 0.25


def interpolate_concern(value: float, anchors: cfg.Anchors) -> float:
    """Piecewise-linear mapping from metric value to 0-100 concern, clamped
    at the outermost anchors."""
    if not anchors:
        raise ValueError("Empty anchors")
    pts = sorted(anchors, key=lambda p: p[0])
    if value <= pts[0][0]:
        return pts[0][1]
    if value >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y0
            frac = (value - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    raise AssertionError("unreachable")


def _direction(score: float) -> Direction:
    """Bands set from the empirical backtest score distribution (v0.3):
    scores compress to ~17-67 in practice, so the original 35/60 bands almost
    never labelled anything negative. positive < p50, negative > p90."""
    if score < cfg.DIRECTION_POSITIVE_BELOW:
        return Direction.POSITIVE
    if score <= cfg.DIRECTION_NEGATIVE_ABOVE:
        return Direction.MIXED
    return Direction.NEGATIVE


def _confidence(coverage: float, n_ok: int) -> Confidence:
    if coverage >= 0.7 and n_ok >= 3:
        return Confidence.HIGH
    if coverage >= 0.4 and n_ok >= 2:
        return Confidence.MEDIUM
    return Confidence.LOW


def _anchors_for(metric_name: str, default: cfg.Anchors, sector: str | None) -> cfg.Anchors:
    if sector and sector in cfg.SECTOR_ANCHOR_OVERRIDES:
        return cfg.SECTOR_ANCHOR_OVERRIDES[sector].get(metric_name, default)
    return default


def score_block(
    spec: cfg.BlockSpec,
    metrics_by_name: dict[str, MetricResult],
    sector: str | None = None,
    extra_caveats: list[str] | None = None,
) -> BlockScore:
    components: list[ComponentContribution] = []
    weighted_sum = 0.0
    weight_ok = 0.0
    weight_total = 0.0

    for ms in spec.metrics:
        weight_total += ms.weight
        m = metrics_by_name.get(ms.metric_name)
        anchors = _anchors_for(ms.metric_name, ms.anchors, sector)
        if m is None or m.status is not MetricStatus.OK or m.value is None:
            components.append(
                ComponentContribution(
                    metric_name=ms.metric_name,
                    metric_value=None,
                    concern_score=None,
                    weight=ms.weight,
                    anchors=anchors,
                    status=m.status.value if m else "not_computed",
                    note=m.note if m else "Metric not computed",
                )
            )
            continue
        concern = interpolate_concern(m.value, anchors)
        weighted_sum += concern * ms.weight
        weight_ok += ms.weight
        components.append(
            ComponentContribution(
                metric_name=ms.metric_name,
                metric_value=m.value,
                concern_score=round(concern, 1),
                weight=ms.weight,
                anchors=anchors,
                status=m.status.value,
                note=m.note,
            )
        )

    coverage = weight_ok / weight_total if weight_total else 0.0
    n_ok = sum(1 for c in components if c.concern_score is not None)
    caveats = [cfg.V0_WEIGHTS_CAVEAT] + list(extra_caveats or [])

    if coverage < MIN_COVERAGE_FOR_SCORE:
        return BlockScore(
            name=spec.name,
            score=None,
            direction=Direction.MIXED,
            confidence=Confidence.LOW,
            rationale=(
                f"Insufficient data: only {n_ok} of {len(spec.metrics)} metrics computed "
                f"({coverage:.0%} of block weight). No score is asserted."
            ),
            components=components,
            caveats=caveats,
            data_coverage=round(coverage, 3),
        )

    score = weighted_sum / weight_ok
    scored = [c for c in components if c.concern_score is not None]
    top = max(scored, key=lambda c: c.concern_score * c.weight)  # type: ignore[operator]
    low = min(scored, key=lambda c: c.concern_score * c.weight)  # type: ignore[operator]
    rationale = (
        f"Score {score:.0f}/100 from {n_ok} metrics ({coverage:.0%} weight coverage). "
        f"Largest concern contributor: {top.metric_name} "
        f"(value {top.metric_value:.3g}, concern {top.concern_score:.0f}). "
        f"Most supportive: {low.metric_name} "
        f"(value {low.metric_value:.3g}, concern {low.concern_score:.0f})."
    )
    return BlockScore(
        name=spec.name,
        score=round(score, 1),
        direction=_direction(score),
        confidence=_confidence(coverage, n_ok),
        rationale=rationale,
        components=components,
        caveats=caveats,
        data_coverage=round(coverage, 3),
    )


def score_overall(block_scores: list[BlockScore]) -> OverallScore:
    weighted_sum = 0.0
    weight_avail = 0.0
    for bs in block_scores:
        w = cfg.BLOCK_WEIGHTS.get(bs.name, 0.0)
        if bs.score is not None:
            weighted_sum += bs.score * w
            weight_avail += w

    caveats = [cfg.V0_WEIGHTS_CAVEAT]
    if weight_avail < 0.5:
        return OverallScore(
            score=None,
            direction=Direction.MIXED,
            confidence=Confidence.LOW,
            block_weights=cfg.BLOCK_WEIGHTS,
            rationale=(
                f"Only {weight_avail:.0%} of block weight had sufficient data; "
                "an overall score is not asserted."
            ),
            caveats=caveats,
        )

    score = weighted_sum / weight_avail
    scored_blocks = [b for b in block_scores if b.score is not None]
    worst = max(scored_blocks, key=lambda b: b.score)  # type: ignore[arg-type, return-value]
    best = min(scored_blocks, key=lambda b: b.score)  # type: ignore[arg-type, return-value]
    confidences = [b.confidence for b in scored_blocks]
    confidence = (
        Confidence.HIGH
        if weight_avail >= 0.8 and confidences.count(Confidence.HIGH) >= len(confidences) / 2
        else Confidence.MEDIUM
        if weight_avail >= 0.6
        else Confidence.LOW
    )
    if weight_avail < 0.999:
        caveats.append(
            f"Overall score renormalized over {weight_avail:.0%} of design weight "
            "due to missing block data."
        )
    return OverallScore(
        score=round(score, 1),
        direction=_direction(score),
        confidence=confidence,
        block_weights=cfg.BLOCK_WEIGHTS,
        rationale=(
            f"Weighted average of {len(scored_blocks)} block scores. "
            f"Highest concern: {worst.name} ({worst.score:.0f}). "
            f"Lowest concern: {best.name} ({best.score:.0f})."
        ),
        caveats=caveats,
    )


def score_all(
    metrics: list[MetricResult],
    sector: str | None = None,
    high_growth: bool = False,
) -> tuple[list[BlockScore], OverallScore]:
    metrics_by_name = {m.name: m for m in metrics}
    blocks: list[BlockScore] = []
    for spec in cfg.BLOCKS:
        extra: list[str] = []
        if high_growth and spec.name in cfg.GROWTH_SENSITIVE_BLOCKS:
            extra.append(cfg.HIGH_GROWTH_CAVEAT)
        if spec.name == "Earnings Quality":
            m = metrics_by_name.get("beneish_m_score")
            if m is not None and m.note:
                extra.append(f"Beneish M-score note: {m.note}")
        blocks.append(score_block(spec, metrics_by_name, sector=sector, extra_caveats=extra))
    return blocks, score_overall(blocks)
