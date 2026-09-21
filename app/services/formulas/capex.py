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
        # Round-15 finding 6 (same class as R14 F1/F2 that missed capex): a
        # negative prior capex slipped through and made `growth()` compute
        # `(cur - prev) / abs(prev)`, producing arithmetically valid but
        # economically meaningless "growth" readings on sign-flipped inputs.
        # Aligned with the `<= 0` convention used everywhere else.
        if prev.capex <= 0:  # type: ignore[operator]
            return "Non-positive prior capex: growth undefined"
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
        # Round-15 finding 6: negative D&A slipped past `== 0` and produced
        # sign-flipped ratios (e.g. capex=20, D&A=-10 → -2.0 as an OK value).
        # D&A is a positive-purchase-convention field per the schema contract.
        guard=lambda: (
            "Non-positive D&A" if cur.depreciation_amortization <= 0 else None  # type: ignore[operator]
        ),
        value_fn=lambda: cur.capex / cur.depreciation_amortization,  # type: ignore[operator]
    )


def capex_intensity_regime_shift(series: list[PeriodFinancials], window: int = 4) -> MetricResult:
    """Mean capex/revenue over the most recent `window` periods minus the mean
    over the preceding periods. Positive = capex intensity has stepped up."""
    label = series[-1].fiscal_label if series else "n/a"
    formula = f"mean(capex/revenue, last {window}) - mean(capex/revenue, prior)"

    def usable(p: PeriodFinancials) -> bool:
        return p.capex is not None and p.revenue is not None and p.revenue > 0

    def unavailable(why: str) -> MetricResult:
        return MetricResult(
            name="capex_intensity_regime_shift",
            formula=formula,
            fiscal_label=label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=[why],
        )

    if not series:
        return unavailable("capex/revenue history (no periods)")

    # This metric compares a RECENT window against everything before it, so
    # dropping unusable periods is not merely a labelling problem: compacting
    # the list slides the window backwards. With Q7 unusable the "last 4"
    # silently became Q3–Q6 while the result still claimed Q7. Both halves of
    # the comparison then describe the wrong span.
    if not usable(series[-1]):
        return unavailable(
            f"capex/revenue in {label} — the most recent period supplied no "
            f"observation, so a 'last {window} quarters' window cannot end there"
        )
    recent_periods = series[-window:]
    if len(recent_periods) < window or not all(usable(p) for p in recent_periods):
        gaps = [p.fiscal_label for p in recent_periods if not usable(p)]
        return unavailable(
            f"capex/revenue for the last {window} periods "
            f"({'gaps at ' + ', '.join(gaps) if gaps else 'insufficient history'}) "
            f"— an incomplete recent window is not a regime"
        )
    prior_periods = [p for p in series[:-window] if usable(p)]
    if len(prior_periods) < 2:
        return unavailable(
            f"capex/revenue history before {recent_periods[0].fiscal_label} "
            f"(need >= 2 usable prior periods to establish a baseline)"
        )

    recent = [p.capex / p.revenue for p in recent_periods]  # type: ignore[operator]
    prior = [p.capex / p.revenue for p in prior_periods]  # type: ignore[operator]
    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    return MetricResult(
        name="capex_intensity_regime_shift",
        formula=formula,
        fiscal_label=label,
        status=MetricStatus.OK,
        value=recent_mean - prior_mean,
        inputs={
            "recent_mean": recent_mean,
            "prior_mean": prior_mean,
            "n_recent": float(len(recent)),
            "n_prior": float(len(prior)),
        },
        note=(f"recent window {recent_periods[0].fiscal_label}–{recent_periods[-1].fiscal_label}; "
              f"baseline {prior_periods[0].fiscal_label}–{prior_periods[-1].fiscal_label}"),
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
