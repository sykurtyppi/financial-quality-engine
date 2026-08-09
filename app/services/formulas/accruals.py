"""Accrual and cash-earnings-reality metrics.

See docs/formula_spec.md §1 for definitions and interpretation guidance.
"""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.base import average, build_metric

MIN_EARNINGS_BASE_NOTE = (
    "Net income is non-positive; ratio-to-earnings is not meaningful. "
    "Review cash flow levels directly."
)


def total_accruals(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    """Sloan-style balance-sheet-free accruals: (NI - CFO) / average total assets."""
    inputs = {
        "net_income": cur.net_income,
        "cfo": cur.cfo,
        "total_assets": cur.total_assets,
        "total_assets_prior": prev.total_assets,
    }
    return build_metric(
        name="total_accruals",
        formula="(Net Income - CFO) / Average Total Assets",
        fiscal_label=cur.fiscal_label,
        inputs=inputs,
        guard=lambda: (
            "Average total assets is non-positive"
            if average(cur.total_assets, prev.total_assets) <= 0  # type: ignore[arg-type]
            else None
        ),
        value_fn=lambda: (cur.net_income - cur.cfo)  # type: ignore[operator]
        / average(cur.total_assets, prev.total_assets),  # type: ignore[arg-type]
    )


def cfo_to_net_income(cur: PeriodFinancials) -> MetricResult:
    inputs = {"cfo": cur.cfo, "net_income": cur.net_income}
    return build_metric(
        name="cfo_to_net_income",
        formula="CFO / Net Income",
        fiscal_label=cur.fiscal_label,
        inputs=inputs,
        # P0-9: a loss AND cash burn is unambiguous distress (max concern). A
        # loss WITH positive operating cash flow is genuinely not an
        # earnings-quality concern, so it stays a benign drop.
        distress_guard=lambda: (
            "Net loss with negative operating cash flow: cash and earnings both "
            "negative; maximum concern"
            if cur.net_income <= 0 and cur.cfo <= 0  # type: ignore[operator]
            else None
        ),
        guard=lambda: MIN_EARNINGS_BASE_NOTE if cur.net_income <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.cfo / cur.net_income,  # type: ignore[operator]
    )


def fcf_to_net_income(cur: PeriodFinancials) -> MetricResult:
    inputs = {"cfo": cur.cfo, "capex": cur.capex, "net_income": cur.net_income}
    return build_metric(
        name="fcf_to_net_income",
        formula="(CFO - Capex) / Net Income",
        fiscal_label=cur.fiscal_label,
        inputs=inputs,
        guard=lambda: MIN_EARNINGS_BASE_NOTE if cur.net_income <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.cfo - cur.capex) / cur.net_income,  # type: ignore[operator]
    )


def fcf_margin(cur: PeriodFinancials) -> MetricResult:
    inputs = {"cfo": cur.cfo, "capex": cur.capex, "revenue": cur.revenue}
    return build_metric(
        name="fcf_margin",
        formula="(CFO - Capex) / Revenue",
        fiscal_label=cur.fiscal_label,
        inputs=inputs,
        guard=lambda: "Revenue is non-positive" if cur.revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.cfo - cur.capex) / cur.revenue,  # type: ignore[operator]
    )


def accrual_trend(series: list[MetricResult]) -> MetricResult:
    """Deterioration in total accruals over the series: latest value minus the
    mean of the prior OK observations. Positive = accruals rising vs history."""
    label = series[-1].fiscal_label if series else "n/a"
    ok = [m for m in series if m.status is MetricStatus.OK and m.value is not None]
    if len(ok) < 3:
        return MetricResult(
            name="accrual_trend",
            formula="latest total_accruals - mean(prior total_accruals)",
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=["total_accruals history (need >= 3 OK periods)"],
        )
    latest = ok[-1].value
    prior_mean = sum(m.value for m in ok[:-1]) / len(ok[:-1])  # type: ignore[misc]
    return MetricResult(
        name="accrual_trend",
        formula="latest total_accruals - mean(prior total_accruals)",
        fiscal_label=label,
        status=MetricStatus.OK,
        value=latest - prior_mean,  # type: ignore[operator]
        inputs={"latest": latest, "prior_mean": prior_mean, "n_prior": float(len(ok) - 1)},
    )
