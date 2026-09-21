"""Shared helpers enforcing the metric contract.

Every formula in this package:
- declares its inputs explicitly,
- returns MISSING_DATA (with the missing field names) if any input is None,
- returns NOT_MEANINGFUL (with a note) instead of dividing by zero or
  producing sign-flipped nonsense,
- never raises on bad data and never silently drops it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

from app.schemas.metrics import MetricResult, MetricStatus

Guard = Callable[[], str | None]


def build_metric(
    name: str,
    formula: str,
    fiscal_label: str,
    inputs: Mapping[str, float | None],
    value_fn: Callable[[], float],
    guard: Guard | None = None,
    distress_guard: Guard | None = None,
    note: str | None = None,
) -> MetricResult:
    """Run one formula under the metric contract.

    `guard` runs after the missing-data check and returns a reason string when
    the computation would be arithmetically valid but economically meaningless
    yet BENIGN (e.g. a loss with positive operating cash flow) — such a metric
    is dropped from scoring.

    `distress_guard` (P0-9) runs first and returns a reason string when the
    ratio is undefined *because the denominator itself signals distress* (a loss
    with cash burn, negative EBITDA with net debt, negative CFO). It is flagged
    `distress_signal=True` so the scoring engine scores it at maximum concern
    instead of dropping it — otherwise the most damning metrics vanish exactly
    in distress and lift the block score.
    """
    missing = sorted(k for k, v in inputs.items() if v is None)
    if missing:
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.MISSING_DATA,
            inputs=dict(inputs),
            missing_fields=missing,
            note=note,
        )
    if distress_guard is not None:
        reason = distress_guard()
        if reason is not None:
            return MetricResult(
                name=name,
                formula=formula,
                fiscal_label=fiscal_label,
                status=MetricStatus.NOT_MEANINGFUL,
                inputs=dict(inputs),
                note=reason,
                distress_signal=True,
            )
    if guard is not None:
        reason = guard()
        if reason is not None:
            return MetricResult(
                name=name,
                formula=formula,
                fiscal_label=fiscal_label,
                status=MetricStatus.NOT_MEANINGFUL,
                inputs=dict(inputs),
                note=reason,
            )
    try:
        value = value_fn()
    except ZeroDivisionError:
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.NOT_MEANINGFUL,
            inputs=dict(inputs),
            note="Division by zero in denominator",
        )
    if not math.isfinite(value):
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.NOT_MEANINGFUL,
            inputs=dict(inputs),
            note="Non-finite result",
        )
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label=fiscal_label,
        status=MetricStatus.OK,
        value=value,
        inputs=dict(inputs),
        note=note,
    )


def growth(current: float, prior: float) -> float:
    """Period-over-period growth. Caller must guard prior != 0; a negative
    prior base makes growth direction ambiguous, so callers should guard
    prior > 0 for revenue-like series."""
    return (current - prior) / abs(prior)


def average(a: float, b: float) -> float:
    return (a + b) / 2.0


def stale_current(
    series: list[MetricResult], name: str, formula: str
) -> MetricResult | None:
    """Refuse a trend whose newest period contributed nothing to it.

    Every trend here labels its result with `series[-1].fiscal_label` — the
    period the caller asked about — but computes over only the OK
    observations. When the newest observation is not OK, those two diverge: a
    value derived entirely from older quarters is returned `OK`, stamped with
    the current quarter, and scored as a current-period signal. Missingness
    silently becomes an apparently valid measurement, the scorecard shows
    false freshness, and a backtest's labelled period does not match the
    information that produced it.

    Returns MISSING_DATA for that case, or None when the newest observation is
    usable and the trend may proceed. The label is kept — the caller did ask
    about this period, and the honest answer is that this period has no value.
    """
    if not series:
        return None
    latest = series[-1]
    if latest.status is MetricStatus.OK and latest.value is not None:
        return None
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label=latest.fiscal_label,
        status=MetricStatus.MISSING_DATA,
        missing_fields=[
            f"{latest.name} in {latest.fiscal_label} "
            f"({latest.status.value}) — a trend cannot be reported for a "
            f"period that supplied no observation"
        ],
    )


def contributor_note(contributors: list[MetricResult]) -> str:
    """Name the periods a trend was actually computed over. Without it the
    reader cannot tell a full window from one with holes in the middle."""
    labels = [m.fiscal_label for m in contributors]
    if not labels:
        return "no contributing periods"
    span = labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"
    return f"computed over {len(labels)} period(s): {span}"
