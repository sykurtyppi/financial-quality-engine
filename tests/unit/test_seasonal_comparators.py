"""P0-B: seasonal comparators — YoY growth spreads and same-fiscal-quarter
trend baselines.

The measured failure class this guards against: sequential-quarter comparison
fabricating spread/trend signals at seasonal boundaries (the Corning Q1 FCF
trough and MSFT June-quarter receivables classes).
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
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


def _values(line: str) -> tuple[float, float]:
    """The two compared values of a change line: `label: A (P) -> B (Q)...`."""
    a = line.split(": ", 1)[1].split(" (")[0]
    b = line.split("-> ", 1)[1].split(" (")[0]
    return float(a), float(b)


class TestChangeLines:
    """The card's "Changes since last period" lines (Hermes: CRM's DSO from
    fiscal Q4 to Q1 is a seasonal comparison, not a change in the business).
    A quarterly ratio of a seasonal balance or flow compares the same fiscal
    quarter a year earlier; the TTM and YoY lines stay sequential."""

    def _lines(self, periods):
        from app.core.pipeline import _what_changed

        ds = CompanyDataset(profile=CompanyProfile(ticker="SEAS"), periods=periods)
        return {ln.split(":", 1)[0]: ln
                for ln in _what_changed(registry.compute_metrics(ds), ds.sorted_periods())}

    def test_a_healthy_seasonal_quarter_shows_no_change(self):
        """Q4 revenue and receivables double in lockstep: DSO against Q3 read
        as a swing, against last year's Q4 it is flat."""
        lines = self._lines(_seasonal_retailer())
        dso = lines["Days sales outstanding"]
        assert "(FY2024Q4) -> " in dso and "(FY2025Q4)" in dso
        assert dso.endswith(", vs the same quarter a year earlier")
        prev, cur = _values(dso)
        assert prev == cur
        capex = lines["Capex / Revenue"]
        assert "(FY2024Q4) -> " in capex and capex.endswith("a year earlier")

    def test_a_real_divergence_still_shows(self):
        periods = _seasonal_retailer()
        periods[-1] = periods[-1].model_copy(update={"receivables": 150.0})
        dso = self._lines(periods)["Days sales outstanding"]
        assert "(FY2024Q4) -> " in dso
        prev, cur = _values(dso)
        assert cur > prev

    def test_without_a_year_ago_quarter_the_line_says_it_is_sequential(self):
        dso = self._lines(_seasonal_retailer()[:4])["Days sales outstanding"]
        assert "(FY2024Q3) -> " in dso and "(FY2024Q4)" in dso
        assert dso.endswith(", sequential quarters: may be seasonal")

    def test_a_year_ago_quarter_outside_the_gap_is_not_used(self):
        """A fiscal-calendar break: four periods back is not a year back."""
        periods = _seasonal_retailer()
        periods[3] = periods[3].model_copy(
            update={"period_end": date(2024, 7, 31), "fiscal_label": "FY2024Q4"})
        periods = sorted(periods, key=lambda p: p.period_end)
        dso = self._lines(periods)["Days sales outstanding"]
        assert dso.endswith(", sequential quarters: may be seasonal")

    def test_a_missing_year_ago_value_falls_back_to_sequential(self):
        periods = _seasonal_retailer()
        periods[3] = periods[3].model_copy(update={"receivables": None})
        dso = self._lines(periods)["Days sales outstanding"]
        assert "(FY2025Q3) -> " in dso
        assert dso.endswith(", sequential quarters: may be seasonal")

    def test_a_fallback_across_a_gap_is_not_called_sequential(self):
        """With no year-ago value and a quarter missing between the last two
        values, they are neither a year apart nor adjacent."""
        periods = _seasonal_retailer()
        for i in (3, 6):  # FY2024Q4 (the year-ago quarter) and FY2025Q3
            periods[i] = periods[i].model_copy(update={"receivables": None})
        dso = self._lines(periods)["Days sales outstanding"]
        assert "(FY2025Q2) -> " in dso and "(FY2025Q4)" in dso
        assert dso.endswith(", not the same quarter a year earlier: may be seasonal")

    def test_annual_periods_carry_no_seasonal_note(self):
        """Consecutive fiscal years already compare the same period a year
        earlier."""
        periods = [
            PeriodFinancials(period_end=date(y, 12, 31), period_type=PeriodType.ANNUAL,
                             fiscal_label=f"FY{y}", revenue=400.0 + y - 2020,
                             receivables=200.0, net_income=40.0, cfo=48.0, capex=20.0,
                             total_assets=1600.0)
            for y in range(2020, 2026)
        ]
        lines = self._lines(periods)
        for label in ("Days sales outstanding", "Capex / Revenue"):
            assert lines[label].endswith("(FY2025)"), lines[label]
            assert "(FY2024) -> " in lines[label]

    def test_the_ttm_and_yoy_lines_are_unchanged(self):
        lines = self._lines(_seasonal_retailer())
        spread = lines["Receivables-vs-revenue growth spread"]
        assert "(FY2025Q3) -> " in spread and spread.endswith("(FY2025Q4)")

    def test_crm_dso_compares_the_same_fiscal_quarter(self):
        """Hermes's example: CRM's fiscal Q1 against the prior fiscal Q1."""
        import json
        from pathlib import Path

        from app.core.pipeline import analyze
        from app.services.ingestion.companyfacts_mapper import build_dataset

        path = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "companyfacts_CRM_trimmed.json"
        ds, _ = build_dataset(json.loads(path.read_text()), "CRM")
        dso = next(c for c in analyze(ds).changes if c.startswith("Days sales outstanding"))
        assert "(FY2026Q1) -> " in dso and "(FY2027Q1)" in dso
        assert dso.endswith(", vs the same quarter a year earlier")
