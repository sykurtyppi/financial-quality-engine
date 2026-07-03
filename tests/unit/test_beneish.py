from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import beneish


def period(label: str, **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.ANNUAL,
        fiscal_label=label,
        **kw,
    )


PREV = period(
    "FY2024",
    revenue=1000.0,
    receivables=100.0,
    cost_of_revenue=600.0,
    current_assets=400.0,
    ppe_net=300.0,
    total_assets=1000.0,
    depreciation_amortization=50.0,
    sga_expense=200.0,
    total_debt=200.0,
    current_liabilities=150.0,
    net_income=100.0,
    cfo=110.0,
)

CUR = period(
    "FY2025",
    revenue=1100.0,
    receivables=143.0,
    cost_of_revenue=715.0,
    current_assets=420.0,
    ppe_net=310.0,
    total_assets=1100.0,
    depreciation_amortization=50.0,
    sga_expense=230.0,
    total_debt=250.0,
    current_liabilities=160.0,
    net_income=120.0,
    cfo=90.0,
)


class TestComponents:
    def test_dsri(self):
        m = beneish.dsri(CUR, PREV)
        # (143/1100) / (100/1000) = 0.13 / 0.10 = 1.3
        assert m.value == pytest.approx(1.3)

    def test_gmi(self):
        m = beneish.gmi(CUR, PREV)
        # GM prev 0.4, GM cur (1100-715)/1100 = 0.35 -> 0.4/0.35
        assert m.value == pytest.approx(0.4 / 0.35)

    def test_aqi(self):
        m = beneish.aqi(CUR, PREV)
        soft_cur = 1 - (420 + 310) / 1100
        soft_prev = 1 - (400 + 300) / 1000
        assert m.value == pytest.approx(soft_cur / soft_prev)

    def test_sgi(self):
        assert beneish.sgi(CUR, PREV).value == pytest.approx(1.1)

    def test_depi(self):
        rate_prev = 50 / (50 + 300)
        rate_cur = 50 / (50 + 310)
        assert beneish.depi(CUR, PREV).value == pytest.approx(rate_prev / rate_cur)

    def test_sgai(self):
        assert beneish.sgai(CUR, PREV).value == pytest.approx((230 / 1100) / (200 / 1000))

    def test_lvgi(self):
        lev_cur = (250 + 160) / 1100
        lev_prev = (200 + 150) / 1000
        assert beneish.lvgi(CUR, PREV).value == pytest.approx(lev_cur / lev_prev)

    def test_tata(self):
        assert beneish.tata(CUR).value == pytest.approx((120 - 90) / 1100)


class TestMScore:
    def test_full_m_score(self):
        results = beneish.compute_all(CUR, PREV)
        m = next(r for r in results if r.name == "beneish_m_score")
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(-1.8886, abs=1e-3)

    def test_missing_component_propagates(self):
        cur = CUR.model_copy(update={"sga_expense": None})
        results = beneish.compute_all(cur, PREV)
        m = next(r for r in results if r.name == "beneish_m_score")
        assert m.status is MetricStatus.MISSING_DATA
        assert "component:sgai" in m.missing_fields
        assert m.value is None

    def test_zero_prior_revenue_guarded(self):
        prev = PREV.model_copy(update={"revenue": 0.0})
        m = beneish.dsri(CUR, prev)
        assert m.status is MetricStatus.NOT_MEANINGFUL
