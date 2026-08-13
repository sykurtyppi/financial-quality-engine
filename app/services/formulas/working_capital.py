"""Working-capital and revenue-quality metrics.

Growth-spread metrics compare balance-sheet build against revenue growth;
day-count metrics (DSO/DIO/DPO) use period length in days derived from the
period type (91 for quarters, 365 for fiscal years).
"""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.base import build_metric, growth


def _days(p: PeriodFinancials) -> float:
    return 91.0 if p.period_type is PeriodType.QUARTER else 365.0


def _growth_spread(
    name: str,
    asset_field: str,
    cur: PeriodFinancials,
    prev: PeriodFinancials,
) -> MetricResult:
    asset_cur: float | None = getattr(cur, asset_field)
    asset_prev: float | None = getattr(prev, asset_field)
    inputs = {
        asset_field: asset_cur,
        f"{asset_field}_prior": asset_prev,
        "revenue": cur.revenue,
        "revenue_prior": prev.revenue,
    }

    def guard() -> str | None:
        if prev.revenue <= 0:  # type: ignore[operator]
            return "Non-positive prior revenue"
        # Round-14 finding 2: `== 0` used to let a negative prior base through
        # (unusual — sign flips from reclassification, consignment inventory,
        # deferred-revenue offsets). `growth()` divides by `abs(prior)`, so a
        # sign flip from -10 to +5 produces "growth = 1.5" — arithmetically
        # valid but economically meaningless, and it scored near max concern.
        # Aligning with the `<= 0` convention used by every other prior-base
        # guard in this codebase.
        if asset_prev <= 0:  # type: ignore[operator]
            return f"Non-positive prior {asset_field}: growth undefined"
        return None

    return build_metric(
        name,
        f"{asset_field} growth - revenue growth",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: growth(asset_cur, asset_prev) - growth(cur.revenue, prev.revenue),  # type: ignore[arg-type]
    )


def receivables_growth_spread(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    return _growth_spread("receivables_growth_spread", "receivables", cur, prev)


def inventory_growth_spread(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    return _growth_spread("inventory_growth_spread", "inventory", cur, prev)


def dso(cur: PeriodFinancials) -> MetricResult:
    inputs = {"receivables": cur.receivables, "revenue": cur.revenue}
    return build_metric(
        "dso",
        "(Receivables / Revenue) * days-in-period",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive revenue" if cur.revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.receivables / cur.revenue) * _days(cur),  # type: ignore[operator]
    )


def dio(cur: PeriodFinancials) -> MetricResult:
    inputs = {"inventory": cur.inventory, "cost_of_revenue": cur.cost_of_revenue}
    return build_metric(
        "dio",
        "(Inventory / COGS) * days-in-period",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive COGS" if cur.cost_of_revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.inventory / cur.cost_of_revenue) * _days(cur),  # type: ignore[operator]
    )


def dpo(cur: PeriodFinancials) -> MetricResult:
    inputs = {"accounts_payable": cur.accounts_payable, "cost_of_revenue": cur.cost_of_revenue}
    return build_metric(
        "dpo",
        "(Accounts Payable / COGS) * days-in-period",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive COGS" if cur.cost_of_revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.accounts_payable / cur.cost_of_revenue) * _days(cur),  # type: ignore[operator]
    )


def deferred_revenue_growth_spread(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    """Deferred revenue growth minus revenue growth. NEGATIVE spread (deferred
    revenue lagging reported revenue) is the concern direction for
    subscription models: it can indicate softening bookings."""
    return _growth_spread("deferred_revenue_growth_spread", "deferred_revenue", cur, prev)


def working_capital_swing_to_income(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    """|Δ(receivables + inventory - payables)| / |net income|: how much of the
    earnings base moved through working capital this period. High values mean
    earnings are heavily working-capital dependent."""
    inputs = {
        "receivables": cur.receivables,
        "inventory": cur.inventory,
        "accounts_payable": cur.accounts_payable,
        "receivables_prior": prev.receivables,
        "inventory_prior": prev.inventory,
        "accounts_payable_prior": prev.accounts_payable,
        "net_income": cur.net_income,
    }

    def swing() -> float:
        wc_cur = cur.receivables + cur.inventory - cur.accounts_payable  # type: ignore[operator]
        wc_prev = prev.receivables + prev.inventory - prev.accounts_payable  # type: ignore[operator]
        return abs(wc_cur - wc_prev)

    return build_metric(
        "working_capital_swing_to_income",
        "|Δ(Receivables + Inventory - Payables)| / |Net Income|",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Zero net income" if cur.net_income == 0 else None,
        value_fn=lambda: swing() / abs(cur.net_income),  # type: ignore[arg-type]
    )


def seasonal_trend_change(name: str, series: list[MetricResult]) -> MetricResult:
    """Latest OK value minus the mean of SAME-FISCAL-QUARTER prior observations
    (positional stride of 4 through the per-period series).

    Day-count levels (DSO/DIO) are strongly seasonal; comparing the latest
    value to an unconditional trailing mean fabricates deterioration at every
    seasonal peak (roadmap P0-B, the MSFT June-quarter receivables class).
    """
    label = series[-1].fiscal_label if series else "n/a"
    formula = "latest - mean(same fiscal quarter, prior years)"
    if (
        not series
        or series[-1].status is not MetricStatus.OK
        or series[-1].value is None
    ):
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=["latest value"],
        )
    latest = series[-1]
    priors = [series[i] for i in range(len(series) - 5, -1, -4)]
    priors_ok = [m for m in priors if m.status is MetricStatus.OK and m.value is not None]
    if not priors_ok:
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=["same-quarter history (need >= 1 prior year)"],
        )
    prior_mean = sum(m.value for m in priors_ok) / len(priors_ok)  # type: ignore[misc]
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label=label,
        status=MetricStatus.OK,
        value=latest.value - prior_mean,  # type: ignore[operator]
        inputs={
            "latest": latest.value,
            "same_quarter_prior_mean": prior_mean,
            "n_prior_years": float(len(priors_ok)),
        },
    )


def trend_change(name: str, series: list[MetricResult], min_periods: int = 3) -> MetricResult:
    """Latest OK value minus mean of prior OK values, for any day-count metric.
    Used for DSO/DIO trend deterioration."""
    label = series[-1].fiscal_label if series else "n/a"
    ok = [m for m in series if m.status is MetricStatus.OK and m.value is not None]
    if len(ok) < min_periods:
        return MetricResult(
            name=name,
            formula="latest - mean(prior)",
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=[f"history (need >= {min_periods} OK periods)"],
        )
    prior = ok[:-1]
    prior_mean = sum(m.value for m in prior) / len(prior)  # type: ignore[misc]
    return MetricResult(
        name=name,
        formula="latest - mean(prior)",
        fiscal_label=label,
        status=MetricStatus.OK,
        value=ok[-1].value - prior_mean,  # type: ignore[operator]
        inputs={"latest": ok[-1].value, "prior_mean": prior_mean},
    )
