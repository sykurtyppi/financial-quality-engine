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
    note: str | None = None,
) -> MetricResult:
    """Run one formula under the metric contract.

    `guard` runs after the missing-data check and returns a reason string when
    the computation would be arithmetically valid but economically meaningless
    (e.g. CFO/NI with negative net income).
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
