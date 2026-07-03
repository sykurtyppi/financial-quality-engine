"""Capex intensity, capital-efficiency, and regime-shift metrics."""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.base import build_metric, growth


def capex_to_revenue(cur: PeriodFinancials) -> MetricResult:
    inputs = {"capex": cur.capex, "revenue": cur.revenue}
    return build_metric(
        "capex_to_revenue",
        "Capex / Revenue",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive revenue" if cur.revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.capex / cur.revenue,  # type: ignore[operator]
    )


def capex_growth_spread(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "capex": cur.capex,
        "capex_prior": prev.capex,
        "revenue": cur.revenue,
        "revenue_prior": prev.revenue,
    }

    def guard() -> str | None:
        if prev.revenue <= 0:  # type: ignore[operator]
            return "Non-positive prior revenue"
        if prev.capex == 0:
            return "Zero prior capex: growth undefined"
        return None

    return build_metric(
        "capex_growth_spread",
        "Capex growth - Revenue growth",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: growth(cur.capex, prev.capex) - growth(cur.revenue, prev.revenue),  # type: ignore[arg-type]
    )


def capex_to_da(cur: PeriodFinancials) -> MetricResult:
    inputs = {"capex": cur.capex, "depreciation_amortization": cur.depreciation_amortization}
    return build_metric(
        "capex_to_da",
        "Capex / D&A",
        cur.fiscal_label,
        inputs,
        guard=lambda: (
            "Zero D&A" if cur.depreciation_amortization == 0 else None
        ),
        value_fn=lambda: cur.capex / cur.depreciation_amortization,  # type: ignore[operator]
    )


def capex_intensity_regime_shift(series: list[PeriodFinancials], window: int = 4) -> MetricResult:
    """Mean capex/revenue over the most recent `window` periods minus the mean
    over the preceding periods. Positive = capex intensity has stepped up."""
    label = series[-1].fiscal_label if series else "n/a"
    ratios: list[float] = []
    for p in series:
        if p.capex is not None and p.revenue is not None and p.revenue > 0:
            ratios.append(p.capex / p.revenue)
    if len(ratios) < window + 2:
        return MetricResult(
            name="capex_intensity_regime_shift",
            formula=f"mean(capex/revenue, last {window}) - mean(capex/revenue, prior)",
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=[f"capex/revenue history (need >= {window + 2} usable periods)"],
        )
    recent = ratios[-window:]
    prior = ratios[:-window]
    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    return MetricResult(
        name="capex_intensity_regime_shift",
        formula=f"mean(capex/revenue, last {window}) - mean(capex/revenue, prior)",
        fiscal_label=label,
        status=MetricStatus.OK,
        value=recent_mean - prior_mean,
        inputs={"recent_mean": recent_mean, "prior_mean": prior_mean},
    )


def incremental_revenue_per_capex(series: list[PeriodFinancials], lookback: int = 4) -> MetricResult:
    """Revenue added over the last `lookback` periods per dollar of capex spent
    over the same span: (Rev_t - Rev_t-n) / sum(Capex_{t-n+1..t}).

    A coarse capital-efficiency proxy: capex often converts to revenue with a
    lag longer than the window, so LOW values are a review prompt, not a verdict.
    """
    label = series[-1].fiscal_label if series else "n/a"
    if len(series) < lookback + 1:
        return MetricResult(
            name="incremental_revenue_per_capex",
            formula=f"(Rev_t - Rev_t-{lookback}) / sum(Capex over last {lookback} periods)",
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=[f"period history (need >= {lookback + 1} periods)"],
        )
    window = series[-(lookback + 1):]
    rev_start, rev_end = window[0].revenue, window[-1].revenue
    capex_values = [p.capex for p in window[1:]]
    missing = []
    if rev_start is None or rev_end is None:
        missing.append("revenue")
    if any(c is None for c in capex_values):
        missing.append("capex")
    if missing:
        return MetricResult(
            name="incremental_revenue_per_capex",
            formula=f"(Rev_t - Rev_t-{lookback}) / sum(Capex over last {lookback} periods)",
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=missing,
        )
    total_capex = sum(capex_values)  # type: ignore[arg-type]
    if total_capex <= 0:
        return MetricResult(
            name="incremental_revenue_per_capex",
            formula=f"(Rev_t - Rev_t-{lookback}) / sum(Capex over last {lookback} periods)",
            fiscal_label=label,
            status=MetricStatus.NOT_MEANINGFUL,
            note="Non-positive cumulative capex over window",
        )
    return MetricResult(
        name="incremental_revenue_per_capex",
        formula=f"(Rev_t - Rev_t-{lookback}) / sum(Capex over last {lookback} periods)",
        fiscal_label=label,
        status=MetricStatus.OK,
        value=(rev_end - rev_start) / total_capex,
        inputs={"revenue_end": rev_end, "revenue_start": rev_start, "total_capex": total_capex},
        note="Capex-to-revenue conversion lags may exceed the window; low values prompt review, not verdicts.",
    )
