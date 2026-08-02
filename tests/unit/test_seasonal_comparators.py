"""P0-B: seasonal comparators — YoY growth spreads and same-fiscal-quarter
trend baselines.

The measured failure class this guards against: sequential-quarter comparison
fabricating spread/trend signals at seasonal boundaries (the Corning Q1 FCF
trough and MSFT June-quarter receivables classes).
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import CompanyDataset, CompanyProfile, PeriodFinancials, PeriodType
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas import registry, working_capital


def _q(year: int, qi: int, **kwargs) -> PeriodFinancials:
    ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    m, d = ends[qi]
    return PeriodFinancials(
        period_end=date(year, m, d),
        period_type=PeriodType.QUARTER,
        fiscal_label=f"FY{year}Q{qi + 1}",
        **kwargs,
    )


def _seasonal_retailer(years: tuple[int, ...] = (2024, 2025)) -> list[PeriodFinancials]:
    """Q4 revenue and receivables are 2x the other quarters, in lockstep —
    a healthy seasonal business with NO real divergence."""
    periods = []
    for y in years:
        for qi in range(4):
            seasonal = 2.0 if qi == 3 else 1.0
            periods.append(
                _q(
                    y,
                    qi,
                    revenue=100.0 * seasonal,
                    receivables=50.0 * seasonal,
                    inventory=30.0 * seasonal,
                    cost_of_revenue=60.0 * seasonal,
                    net_income=10.0,
                    cfo=12.0,
                    capex=5.0 * seasonal,
                    total_assets=400.0,
                )
            )
    return periods


class TestYoySpreads:
    def _bundle(self, periods):
        ds = CompanyDataset(profile=CompanyProfile(ticker="SEAS"), periods=periods)
        return registry.compute_metrics(ds)

    def test_healthy_seasonal_business_shows_zero_spread(self):
        """The core P0-B assertion: lockstep seasonality is NOT divergence.
        QoQ comparison would report a +100% receivables 'spread' every Q4."""
        bundle = self._bundle(_seasonal_retailer())
        m = bundle.get_latest("receivables_growth_spread")
        assert m is not None and m.status is MetricStatus.OK
        assert abs(m.value) < 1e-9
        assert "YoY basis" in (m.note or "")

    def test_real_yoy_divergence_still_detected(self):
        periods = _seasonal_retailer()
        # Latest Q4: receivables balloon 50% beyond the seasonal norm.
        periods[-1] = periods[-1].model_copy(update={"receivables": 150.0})
        bundle = self._bundle(periods)
        m = bundle.get_latest("receivables_growth_spread")
        assert m is not None and m.status is MetricStatus.OK
        assert abs(m.value - 0.5) < 1e-9

    def test_no_year_ago_quarter_degrades_explicitly(self):
        bundle = self._bundle(_seasonal_retailer()[:4])
        m = bundle.get_latest("receivables_growth_spread")
        assert m is not None and m.status is MetricStatus.MISSING_DATA
        assert m.missing_fields == [registry.YOY_BASELINE_MISSING]

    def test_capex_spread_is_yoy(self):
        bundle = self._bundle(_seasonal_retailer())
        m = bundle.get_latest("capex_growth_spread")
        assert m is not None and m.status is MetricStatus.OK
        assert abs(m.value) < 1e-9


class TestSeasonalTrendChange:
    def _series(self, values: list[float | None]) -> list[MetricResult]:
        out = []
        for i, v in enumerate(values):
            ok = v is not None
            out.append(
                MetricResult(
                    name="dso",
                    formula="x",
                    fiscal_label=f"P{i}",
                    status=MetricStatus.OK if ok else MetricStatus.MISSING_DATA,
                    value=v,
                )
            )
        return out

    def test_compares_same_fiscal_quarter_only(self):
        """Seasonal series 40/40/40/80 repeating: latest Q4=88 vs prior Q4
        mean 80 -> +8. The old unconditional trailing mean (~48.6) would have
        reported +39 — fabricated deterioration."""
        series = self._series([40, 40, 40, 80, 40, 40, 40, 88])
        m = working_capital.seasonal_trend_change("dso_trend", series)
        assert m.status is MetricStatus.OK
        assert abs(m.value - 8.0) < 1e-9
        assert m.inputs["n_prior_years"] == 1.0

    def test_two_prior_years_averaged(self):
        series = self._series([40, 40, 40, 80, 40, 40, 40, 90, 40, 40, 40, 88])
        m = working_capital.seasonal_trend_change("dso_trend", series)
        assert m.status is MetricStatus.OK
        assert abs(m.value - (88.0 - 85.0)) < 1e-9  # mean(80, 90) = 85

    def test_no_same_quarter_history_is_missing(self):
        series = self._series([40, 42, 44])
        m = working_capital.seasonal_trend_change("dso_trend", series)
        assert m.status is MetricStatus.MISSING_DATA
        assert "same-quarter history" in m.missing_fields[0]

    def test_missing_latest_is_missing(self):
        series = self._series([40, 40, 40, 80, None])
        m = working_capital.seasonal_trend_change("dso_trend", series)
        assert m.status is MetricStatus.MISSING_DATA
