from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.schemas.metrics import MetricStatus
from app.services.market_data import MarketSnapshot
from app.services.valuation import compute_trailing_valuation


def _dataset(*, net_income: float = 10.0, cfo: float = 20.0) -> CompanyDataset:
    periods = [
        PeriodFinancials(
            period_end=date(2025, month, 28),
            period_type=PeriodType.QUARTER,
            fiscal_label=f"FY2025Q{quarter}",
            revenue=100.0,
            net_income=net_income,
            ebit=15.0,
            depreciation_amortization=5.0,
            cfo=cfo,
            capex=5.0,
            total_debt=50.0,
            cash_and_equivalents=20.0,
            shares_outstanding=10.0,
        )
        for quarter, month in enumerate((3, 6, 9, 12), start=1)
    ]
    return CompanyDataset(
        profile=CompanyProfile(
            ticker="TEST", name="Test Co", reporting_currency="USD"
        ),
        periods=periods,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        ticker="TEST",
        price=30.0,
        previous_close=29.0,
        currency="USD",
        exchange="NMS",
        observed_at=datetime(2026, 10, 13, 20, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        market_session="CLOSED",
        provider="Yahoo Finance",
        delay_seconds=0,
        is_delayed=False,
    )


def test_trailing_valuation_calculates_disclosed_ratios_from_market_and_sec_inputs() -> None:
    valuation = compute_trailing_valuation(_dataset(), _snapshot())

    assert valuation.fundamentals_source == "SEC filings (normalized Companyfacts)"
    assert valuation.period_basis == "TTM: four latest reported quarters"
    assert valuation.financial_period_end == date(2025, 12, 28)
    assert valuation.shares_basis == "Latest reported point-in-time shares outstanding"
    assert valuation.market_snapshot.provider == "Yahoo Finance"

    expected = {
        "market_cap": 300.0,
        "trailing_pe": 7.5,
        "price_to_sales": 0.75,
        "enterprise_value": 330.0,
        "ev_to_ebitda": 4.125,
        "price_to_fcf": 5.0,
        "fcf_yield": 0.2,
        "earnings_yield": 40.0 / 300.0,
        "net_debt_to_ebitda": 0.375,
    }
    assert set(valuation.metrics) == set(expected)
    for name, expected_value in expected.items():
        metric = valuation.metrics[name]
        assert metric.status is MetricStatus.OK
        assert metric.value == pytest.approx(expected_value)
        assert metric.formula
        assert metric.inputs


def test_loss_company_ratios_are_not_meaningful_instead_of_sign_flipped() -> None:
    valuation = compute_trailing_valuation(
        _dataset(net_income=-10.0, cfo=0.0),
        _snapshot(),
    )

    for name in ("trailing_pe", "price_to_fcf", "earnings_yield"):
        metric = valuation.metrics[name]
        assert metric.status is MetricStatus.NOT_MEANINGFUL
        assert metric.value is None
        assert metric.note is not None

    assert valuation.metrics["price_to_sales"].status is MetricStatus.OK
    assert valuation.metrics["fcf_yield"].status is MetricStatus.OK
    assert valuation.metrics["fcf_yield"].value == pytest.approx(-20.0 / 300.0)


def test_market_snapshot_must_match_fundamentals_ticker() -> None:
    snapshot = _snapshot().model_copy(update={"ticker": "OTHER"})

    with pytest.raises(ValueError, match="ticker mismatch"):
        compute_trailing_valuation(_dataset(), snapshot)


def test_market_snapshot_currency_must_match_filing_currency() -> None:
    snapshot = _snapshot().model_copy(update={"currency": "EUR"})

    with pytest.raises(ValueError, match="currency mismatch"):
        compute_trailing_valuation(_dataset(), snapshot)


def test_filing_currency_must_be_known() -> None:
    dataset = _dataset()
    dataset.profile.reporting_currency = None

    with pytest.raises(ValueError, match="filing currency unavailable"):
        compute_trailing_valuation(dataset, _snapshot())


def test_non_finite_derived_valuation_is_rejected() -> None:
    snapshot = _snapshot().model_copy(update={"price": 1e308})

    with pytest.raises(ValueError, match="non-finite valuation metric"):
        compute_trailing_valuation(_dataset(), snapshot)


def test_missing_shares_propagate_to_market_cap_dependent_metrics() -> None:
    dataset = _dataset()
    dataset.periods[-1].shares_outstanding = None

    valuation = compute_trailing_valuation(dataset, _snapshot())

    assert valuation.metrics["market_cap"].status is MetricStatus.MISSING_DATA
    assert valuation.metrics["trailing_pe"].status is MetricStatus.MISSING_DATA
    assert valuation.metrics["earnings_yield"].status is MetricStatus.MISSING_DATA
