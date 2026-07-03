"""Runs every formula over a company dataset and returns a traceable bundle:
latest-period metrics (what gets scored) plus full per-period history
(what the report uses for change detection)."""

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
    working_capital,
)

MIN_PERIODS = 2


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
    return [
        accruals.total_accruals(cur, prev),
        accruals.cfo_to_net_income(cur),
        accruals.fcf_to_net_income(cur),
        accruals.fcf_margin(cur),
        working_capital.receivables_growth_spread(cur, prev),
        working_capital.inventory_growth_spread(cur, prev),
        working_capital.deferred_revenue_growth_spread(cur, prev),
        working_capital.dso(cur),
        working_capital.dio(cur),
        working_capital.dpo(cur),
        working_capital.working_capital_swing_to_income(cur, prev),
        balance_sheet.net_debt_to_ebitda(cur),
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
        capex.capex_growth_spread(cur, prev),
        capex.capex_to_da(cur),
    ]


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
        pair = _pair_metrics(cur, prev) + beneish.compute_all(cur, prev)
        for m in pair:
            history.setdefault(m.name, []).append(m)
        if i == len(periods) - 1:
            latest.extend(pair)

    # Series-level metrics computed on full history
    series_metrics = [
        accruals.accrual_trend(history.get("total_accruals", [])),
        working_capital.trend_change("dso_trend", history.get("dso", [])),
        working_capital.trend_change("dio_trend", history.get("dio", [])),
        working_capital.trend_change("fcf_margin_trend", history.get("fcf_margin", [])),
        capex.capex_intensity_regime_shift(periods),
        capex.incremental_revenue_per_capex(periods),
    ]
    for m in series_metrics:
        history.setdefault(m.name, []).append(m)
    latest.extend(series_metrics)

    return MetricsBundle(latest=latest, history=history)
