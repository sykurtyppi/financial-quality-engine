"""Multiples are shown only where they mean something; otherwise the reason
is on the line (thesis_monitor_architecture: earnings multiples are
undefined on the names the distress lens is validated on)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.services.valuation.bridge import enterprise_value_bridge
from app.services.valuation.multiples import (
    HISTORY_LINE,
    PEER_LINE,
    compute_multiples,
    trailing,
)
from app.services.valuation.observation import MarketObservation

ENDS = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))
OBS = MarketObservation(ticker="SYN", price=50.0, currency="USD",
                        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
                        source="test", recorded_at=datetime(2026, 2, 2, tzinfo=UTC))


def _dataset(n: int = 4, **overrides) -> CompanyDataset:
    base = dict(revenue=1000.0, net_income=100.0, ebit=150.0, depreciation_amortization=50.0,
                cfo=200.0, capex=50.0, cash_and_equivalents=100.0, total_debt=300.0,
                shares_outstanding=10.0)
    base.update(overrides)
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER,
                                fiscal_label=f"FY2025Q{i + 1}", **base)
               for i, e in enumerate(ENDS[-n:])]
    return CompanyDataset(profile=CompanyProfile(ticker="SYN"), periods=periods)


def _multiples(ds: CompanyDataset):
    b = enterprise_value_bridge(ds, OBS)
    ttm = trailing(ds, b)
    return b, ttm, {m.name: m for m in compute_multiples(b, ttm)}


def test_every_multiple_reconciles_to_ttm_figures():
    b, ttm, m = _multiples(_dataset())
    assert (b.market_cap.value, b.ev) == (500.0, 700.0)
    assert ttm.label == "TTM FY2025Q4" and ttm.reason is None
    assert (ttm.revenue, ttm.net_income, ttm.ebit, ttm.ebitda, ttm.fcf) == (4000, 400, 600, 800, 600)
    expected = {
        "P/E": 500 / 400, "EV/EBIT": 700 / 600, "EV/EBITDA": 700 / 800, "EV/Sales": 700 / 4000,
        "P/S": 500 / 4000, "P/FCF": 500 / 600, "FCF yield": 600 / 500,
        "earnings yield": 400 / 500,
    }
    assert set(m) == set(expected)
    for name, value in expected.items():
        assert m[name].value == pytest.approx(value), name
        assert m[name].reason is None
        assert m[name].ttm_window == "TTM FY2025Q4"
    assert m["P/E"].numerator == 500.0 and m["P/E"].denominator == 400.0
    assert m["FCF yield"].numerator == 600.0 and m["FCF yield"].denominator == 500.0


def test_negative_net_income_makes_pe_not_meaningful_with_the_reason():
    _, _, m = _multiples(_dataset(net_income=-300.0))
    assert m["P/E"].value is None
    assert m["P/E"].reason == "TTM net income is negative (-1,200 USD): P/E undefined"
    # A yield on a positive market cap is still a number, a negative one.
    assert m["earnings yield"].value == pytest.approx(-1200 / 500)
    assert m["EV/EBIT"].value is not None


def test_zero_or_negative_ebitda():
    _, _, m = _multiples(_dataset(ebit=-50.0, depreciation_amortization=50.0))
    assert m["EV/EBITDA"].value is None
    assert m["EV/EBITDA"].reason == "TTM EBITDA is zero (0 USD): EV/EBITDA undefined"
    assert m["EV/EBIT"].reason == "TTM EBIT is negative (-200 USD): EV/EBIT undefined"


def test_missing_denominator():
    _, _, m = _multiples(_dataset(depreciation_amortization=None))
    assert m["EV/EBITDA"].value is None and m["EV/EBITDA"].reason == "TTM EBITDA missing"
    assert m["EV/EBIT"].value is not None


def test_incomplete_ttm_window_propagates_to_every_multiple():
    _, ttm, m = _multiples(_dataset(n=3))
    assert ttm.label is None
    assert ttm.reason == "TTM window incomplete: fewer than 4 consecutive quarters ending FY2025Q3"
    for mult in m.values():
        assert mult.value is None and mult.reason == ttm.reason and mult.ttm_window is None


def test_ev_not_asserted_propagates_to_the_ev_multiples_only():
    b, _, m = _multiples(_dataset(total_debt=None))
    assert b.ev is None
    for name in ("EV/EBIT", "EV/EBITDA", "EV/Sales"):
        assert m[name].value is None and m[name].reason == b.ev_reason
    assert m["P/E"].value == pytest.approx(500 / 400)
    assert m["P/S"].value == pytest.approx(500 / 4000)


def test_no_market_cap_leaves_nothing_meaningful():
    b, _, m = _multiples(_dataset(shares_outstanding=None))
    assert b.market_cap.value is None
    assert all(x.value is None and x.reason == b.ev_reason for x in m.values())


def test_ranges_are_declared_unavailable_not_faked():
    assert HISTORY_LINE.startswith("own-history range: not available")
    assert "no price history recorded" in HISTORY_LINE
    assert PEER_LINE == "peer range: no reference class (none defined)"
