"""Runs every formula over a company dataset and returns a traceable bundle:
latest-period metrics (what gets scored) plus full per-period history
(what the report uses for change detection).

Basis discipline (roadmap P0-A/P0-B): stock-over-flow ratios and
annually-anchored models (Sloan accruals, Beneish) are computed on
TTM-annualized views from `ttm.annualize`, never on a single quarter's flow;
seasonal growth spreads compare to the same fiscal quarter one year earlier,
never the sequential quarter. Only same-period flow/flow ratios, instant
ratios, and non-seasonal stock changes remain quarterly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.financials import CompanyDataset, PeriodFinancials
from app.schemas.metrics import MetricResult
from app.services.formulas import (
    accruals,
    balance_sheet,
    beneish,
    capex,
    capital_structure,
    ttm,
    working_capital,
)

MIN_PERIODS = 2

TTM_NOTE = "TTM basis: flows summed over the 4 quarters ending this period."
YOY_NOTE = "YoY basis: compared to the same fiscal quarter one year earlier."
YOY_BASELINE_MISSING = "yoy_baseline (same quarter last year)"

# Accepts 52/53-week fiscal years; rejects a mislabeled or skipped year.
MIN_YEAR_GAP_DAYS = 330
MAX_YEAR_GAP_DAYS = 400

#: Metrics that must never be computed on a single quarter's flow. The
#: basis-lint test asserts registry wiring routes these through ttm.annualize.
TTM_BASIS_METRICS = frozenset(
    {
        "total_accruals",
        "cfo_to_net_income",
        "fcf_to_net_income",
        "fcf_margin",
        "net_debt_to_ebitda",
        "beneish_dsri",
        "beneish_gmi",
        "beneish_aqi",
        "beneish_sgi",
        "beneish_depi",
        "beneish_sgai",
        "beneish_lvgi",
        "beneish_tata",
        "beneish_m_score",
    }
)


class MetricsBundle(BaseModel):
    latest: list[MetricResult] = Field(default_factory=list)
    history: dict[str, list[MetricResult]] = Field(
        default_factory=dict,
        description="metric name -> per-period results, ascending order",
    )

    def get_latest(self, name: str) -> MetricResult | None:
        for m in self.latest:
            if m.name == name:
                return m
        return None


def _pair_metrics(cur: PeriodFinancials, prev: PeriodFinancials) -> list[MetricResult]:
    """Quarterly-basis metrics: same-period flow/flow ratios, instant ratios,
    and QoQ changes in non-seasonal stocks. Nothing here divides a stock by a
    sub-annual flow, and nothing here compares seasonal flows QoQ."""
    return [
        working_capital.dso(cur),
        working_capital.dio(cur),
        working_capital.dpo(cur),
        working_capital.working_capital_swing_to_income(cur, prev),
        balance_sheet.interest_coverage(cur),
        balance_sheet.debt_to_assets(cur),
        balance_sheet.current_ratio(cur),
        balance_sheet.asset_quality_proxy(cur),
        balance_sheet.intangibles_to_assets(cur),
        balance_sheet.goodwill_growth(cur, prev),
        balance_sheet.leverage_change(cur, prev),
        capital_structure.sbc_to_revenue(cur),
        capital_structure.sbc_to_cfo(cur),
        capital_structure.diluted_share_growth(cur, prev),
        capital_structure.net_share_count_change(cur, prev),
        capital_structure.buyback_offset_ratio(cur),
        capital_structure.issuance_pressure(cur),
        capex.capex_to_revenue(cur),
        capex.capex_to_da(cur),
    ]


def _year_ago(periods: list[PeriodFinancials], i: int) -> PeriodFinancials | None:
    if i < 4:
        return None
    prior = periods[i - 4]
    gap = (periods[i].period_end - prior.period_end).days
    return prior if MIN_YEAR_GAP_DAYS <= gap <= MAX_YEAR_GAP_DAYS else None


def _yoy_metrics(periods: list[PeriodFinancials], i: int) -> list[MetricResult]:
    """Seasonal growth spreads compared to the same fiscal quarter one year
    earlier. Sequential-quarter comparison fabricated spread signals at every
    seasonal boundary (roadmap P0-B)."""
    cur = periods[i]
    prior = _year_ago(periods, i)

    def spreads(prev: PeriodFinancials) -> list[MetricResult]:
        return [
            working_capital.receivables_growth_spread(cur, prev),
            working_capital.inventory_growth_spread(cur, prev),
            working_capital.deferred_revenue_growth_spread(cur, prev),
            capex.capex_growth_spread(cur, prev),
        ]

    if prior is None:
        stub = PeriodFinancials(
            period_end=cur.period_end,
            period_type=cur.period_type,
            fiscal_label=cur.fiscal_label,
        )
        return [
            m.model_copy(update={"missing_fields": [YOY_BASELINE_MISSING]})
            for m in spreads(stub)
        ]
    return [_with_note(m, YOY_NOTE) for m in spreads(prior)]


def _empty_all(cur: PeriodFinancials) -> PeriodFinancials:
    """A shell period carrying only dates/labels, used so formulas can emit
    correctly-named MISSING_DATA results when no TTM window exists."""
    return PeriodFinancials(
        period_end=cur.period_end,
        period_type=cur.period_type,
        fiscal_label=f"{ttm.TTM_LABEL_PREFIX}{cur.fiscal_label}",
    )


def _mark_window_missing(m: MetricResult) -> MetricResult:
    return m.model_copy(update={"missing_fields": [ttm.TTM_WINDOW_MISSING]})


def _with_note(m: MetricResult, extra: str) -> MetricResult:
    note = f"{m.note} {extra}".strip() if m.note else extra
    return m.model_copy(update={"note": note})


def _ttm_metrics(periods: list[PeriodFinancials], i: int) -> list[MetricResult]:
    """Annual-basis metrics at index i: TTM flows, quarter-end instants,
    Beneish on year-over-year TTM windows."""
    cur = periods[i]
    ttm_cur = ttm.annualize(periods, i)

    if ttm_cur is None:
        stub = _empty_all(cur)
        out = [
            accruals.cfo_to_net_income(stub),
            accruals.fcf_to_net_income(stub),
            accruals.fcf_margin(stub),
            balance_sheet.net_debt_to_ebitda(stub),
            accruals.total_accruals(stub, stub),
            *beneish.compute_all(stub, stub),
        ]
        return [_mark_window_missing(m) for m in out]

    # Sloan average-assets base: quarter-end now vs one year ago. The prior
    # observation must be date-validated — an unvalidated periods[i-4] can be
    # years stale across a history gap and silently poison the asset average
    # (review finding on PR #2). No valid year-ago instant -> MISSING, never
    # a substituted base.
    prior_year_q = _year_ago(periods, i)
    if prior_year_q is not None:
        accruals_m = accruals.total_accruals(ttm_cur, prior_year_q)
    else:
        accruals_m = accruals.total_accruals(ttm_cur, _empty_all(cur)).model_copy(
            update={"missing_fields": [ttm.PRIOR_YEAR_MISSING]}
        )
    out = [
        accruals.cfo_to_net_income(ttm_cur),
        accruals.fcf_to_net_income(ttm_cur),
        accruals.fcf_margin(ttm_cur),
        balance_sheet.net_debt_to_ebitda(ttm_cur),
        accruals_m,
    ]

    ttm_prev = ttm.annualize(periods, i - 4)
    if ttm_prev is not None:
        out += beneish.compute_all(ttm_cur, ttm_prev)
    else:
        stub = _empty_all(cur)
        out += [_mark_window_missing(m) for m in beneish.compute_all(stub, stub)]

    return [_with_note(m, TTM_NOTE) for m in out]


def compute_metrics(dataset: CompanyDataset) -> MetricsBundle:
    periods = dataset.sorted_periods()
    if len(periods) < MIN_PERIODS:
        raise ValueError(
            f"Need at least {MIN_PERIODS} periods, got {len(periods)}. "
            "Period-over-period analysis is the core of the engine."
        )

    history: dict[str, list[MetricResult]] = {}
    latest: list[MetricResult] = []

    for i in range(1, len(periods)):
        cur, prev = periods[i], periods[i - 1]
        # TTM metrics first: preserves the long-standing flag tie-break order
        # (accrual/cash-conversion evidence outranks later families at equal concern).
        pair = (
            _ttm_metrics(periods, i)
            + _yoy_metrics(periods, i)
            + _pair_metrics(cur, prev)
        )
        for m in pair:
            history.setdefault(m.name, []).append(m)
        if i == len(periods) - 1:
            latest.extend(pair)

    # Series-level metrics computed on full history
    series_metrics = [
        accruals.accrual_trend(history.get("total_accruals", [])),
        working_capital.seasonal_trend_change("dso_trend", history.get("dso", [])),
        working_capital.seasonal_trend_change("dio_trend", history.get("dio", [])),
        working_capital.trend_change("fcf_margin_trend", history.get("fcf_margin", [])),
        capex.capex_intensity_regime_shift(periods),
        capex.incremental_revenue_per_capex(periods),
    ]
    for m in series_metrics:
        history.setdefault(m.name, []).append(m)
    latest.extend(series_metrics)

    return MetricsBundle(latest=latest, history=history)
