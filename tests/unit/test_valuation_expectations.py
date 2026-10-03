"""Implied expectations are model arithmetic over the bridge and the TTM
figures: every assumption is explicit, defaults are labelled as defaults,
and a case with no solution says so instead of printing a number."""

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
from app.services.valuation.expectations import (
    compute_expectations,
    gordon_growth,
    implied_growth,
    present_value,
)
from app.services.valuation.multiples import trailing
from app.services.valuation.observation import Assumptions, MarketObservation, Scenario

ENDS = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))
A = Assumptions(required_return=0.09, terminal_growth=0.025, horizon_years=10)


def _pv_by_hand(fcf, g, years, tg, r):
    pv = 0.0
    f = fcf
    for t in range(1, years + 1):
        f *= 1 + g
        pv += f / (1 + r) ** t
    terminal = f * (1 + tg) / (r - tg)
    return pv + terminal / (1 + r) ** years


def test_present_value_matches_a_hand_computation():
    assert present_value(100.0, 0.05, 10, 0.025, 0.09) == pytest.approx(
        _pv_by_hand(100.0, 0.05, 10, 0.025, 0.09))
    assert present_value(100.0, -0.2, 3, 0.0, 0.12) == pytest.approx(
        _pv_by_hand(100.0, -0.2, 3, 0.0, 0.12))


@pytest.mark.parametrize("g", [-0.3, -0.05, 0.0, 0.05, 0.3, 0.8])
def test_bisection_recovers_a_known_growth(g):
    ev = present_value(100.0, g, A.horizon_years, A.terminal_growth, A.required_return)
    found = implied_growth(100.0, ev, A)
    assert found.reason is None
    assert found.value == pytest.approx(g, abs=1e-9)
    assert "bisection" in found.formula and "10 years" in found.formula


def test_implied_growth_is_monotone_in_price():
    values = [implied_growth(100.0, ev, A).value for ev in (1000.0, 1500.0, 2000.0, 4000.0)]
    assert values == sorted(values) and len(set(values)) == 4


@pytest.mark.parametrize("fcf", [0.0, -50.0])
def test_no_solution_when_fcf_is_not_positive(fcf):
    r = implied_growth(fcf, 1000.0, A)
    assert r.value is None and r.reason == "implied growth not computable: TTM FCF ≤ 0"
    g = gordon_growth(fcf, 1000.0, A.required_return)
    assert g.value is None and g.reason == "implied growth not computable: TTM FCF ≤ 0"


def test_no_solution_when_ev_is_not_positive():
    r = implied_growth(100.0, -5.0, A)
    assert r.value is None and r.reason == "implied growth not computable: EV ≤ 0"
    assert gordon_growth(100.0, 0.0, 0.09).reason == "implied growth not computable: EV ≤ 0"


def test_no_solution_outside_the_bracket():
    r = implied_growth(1.0, 1e12, A)
    assert r.value is None and "above +100%/yr" in r.reason
    r = implied_growth(1e12, 1.0, A)
    assert r.value is None and "below -99%/yr" in r.reason
    assert "[-99%, +100%]" in r.formula


def test_the_bracket_ends_are_solutions_not_refusals():
    for g in (-0.99, 1.0):
        ev = present_value(100.0, g, A.horizon_years, A.terminal_growth, A.required_return)
        found = implied_growth(100.0, ev, A)
        assert found.reason is None and found.value == pytest.approx(g, abs=1e-9)


def test_ev_of_exactly_zero_is_refused_everywhere():
    assert implied_growth(100.0, 0.0, A).reason == "implied growth not computable: EV ≤ 0"


def test_gordon_states_its_formula():
    g = gordon_growth(100.0, 2000.0, 0.09)
    assert g.value == pytest.approx(0.09 - 100.0 / 2000.0)
    assert g.formula == "g = r − FCF_ttm / EV"


# --- the whole block over a bridge -------------------------------------------------


def _dataset(**overrides) -> CompanyDataset:
    base = dict(revenue=1000.0, net_income=100.0, ebit=150.0, depreciation_amortization=50.0,
                cfo=200.0, capex=50.0, cash_and_equivalents=100.0, total_debt=300.0,
                shares_outstanding=10.0)
    base.update(overrides)
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER,
                                fiscal_label=f"FY2025Q{i + 1}", **base) for i, e in enumerate(ENDS)]
    return CompanyDataset(profile=CompanyProfile(ticker="SYN"), periods=periods)


def _obs(price=50.0, **kw) -> MarketObservation:
    return MarketObservation(ticker="SYN", price=price, currency="USD",
                             observed_at=datetime(2026, 2, 1, tzinfo=UTC), source="test",
                             recorded_at=datetime(2026, 2, 2, tzinfo=UTC), **kw)


def _expectations(ds: CompanyDataset, obs: MarketObservation):
    b = enterprise_value_bridge(ds, obs)
    return b, compute_expectations(b, trailing(ds, b), obs)


def test_defaults_are_used_and_labelled_only_when_the_file_carries_none():
    ds = _dataset()
    _, e = _expectations(ds, _obs())
    assert e.defaulted is True and e.assumptions == Assumptions()
    own = Assumptions(required_return=0.12, terminal_growth=0.03, horizon_years=5)
    _, e = _expectations(ds, _obs(assumptions=own))
    assert e.defaulted is False and e.assumptions == own


def test_implied_growth_over_the_bridge_and_the_sensitivities():
    ds = _dataset()  # mcap 500, EV 700, TTM FCF 600
    b, e = _expectations(ds, _obs())
    assert e.gordon.value == pytest.approx(0.09 - 600 / 700)
    assert e.reverse.value == pytest.approx(implied_growth(600.0, 700.0, A).value)
    by_name = {s.name: s for s in e.sensitivities}
    assert set(by_name) == {"required return ± 1pt", "price ± 10%"}
    r = by_name["required return ± 1pt"]
    assert r.low == pytest.approx(implied_growth(600.0, 700.0, Assumptions(required_return=0.08)).value)
    assert r.high == pytest.approx(implied_growth(600.0, 700.0, Assumptions(required_return=0.10)).value)
    p = by_name["price ± 10%"]
    # ±10% on the price moves EV by ±10% of the market cap.
    assert p.low == pytest.approx(implied_growth(600.0, 700.0 - 50.0, A).value)
    assert p.high == pytest.approx(implied_growth(600.0, 700.0 + 50.0, A).value)
    assert all(s.swing == pytest.approx(max(abs(s.low - e.reverse.value),
                                            abs(s.high - e.reverse.value)))
               for s in e.sensitivities)
    larger = max(e.sensitivities, key=lambda s: s.swing)
    assert e.main_assumption is not None and e.main_assumption.startswith(larger.name)


def test_sensitivities_are_withheld_when_the_base_case_has_none():
    _, e = _expectations(_dataset(cfo=0.0), _obs())  # TTM FCF = -200
    assert e.reverse.value is None and e.gordon.value is None
    assert e.sensitivities == () and e.main_assumption is None


def test_scenario_value_per_share_reconciles_with_a_hand_computation():
    ds = _dataset()
    scenarios = (Scenario(name="base", fcf_growth=0.05, years=10),
                 Scenario(name="bear", fcf_growth=-0.1, years=3, terminal_growth=0.0,
                          required_return=0.12))
    b, e = _expectations(ds, _obs(scenarios=scenarios))
    net_claims = b.ev - b.market_cap.value  # debt − cash − STI + MI + preferred
    by_name = {s.name: s for s in e.scenarios}
    base = by_name["base"]
    pv = present_value(600.0, 0.05, 10, A.terminal_growth, A.required_return)
    assert base.value_per_share == pytest.approx((pv - net_claims) / 10.0)
    assert base.upside == pytest.approx(base.value_per_share / 50.0 - 1)
    assert base.reason is None and base.label.startswith("model assumption: base")
    bear = by_name["bear"]
    pv = present_value(600.0, -0.1, 3, 0.0, 0.12)
    assert bear.value_per_share == pytest.approx((pv - net_claims) / 10.0)
    assert "r=12.0%" in bear.label and "terminal 0.0%" in bear.label


def test_a_scenario_whose_terminal_growth_equals_r_is_not_valued():
    _, e = _expectations(_dataset(), _obs(scenarios=(Scenario(name="flat", fcf_growth=0.0, years=2,
                                                            terminal_growth=0.09),)))
    (s,) = e.scenarios
    assert s.value_per_share is None
    assert s.reason == "not computable: terminal growth 9.0% is not below r=9.0%"


def test_a_scenario_on_zero_fcf_is_not_valued():
    _, e = _expectations(_dataset(cfo=50.0, capex=50.0),
                         _obs(scenarios=(Scenario(name="s", fcf_growth=0.0, years=2),)))
    (s,) = e.scenarios
    assert s.value_per_share is None and s.reason == "not computable: TTM FCF ≤ 0"


def test_scenarios_without_fcf_or_shares_say_why():
    _, e = _expectations(_dataset(cfo=0.0), _obs(scenarios=(Scenario(name="s", fcf_growth=0.0, years=2),)))
    (s,) = e.scenarios
    assert s.value_per_share is None and s.reason == "not computable: TTM FCF ≤ 0"
    _, e = _expectations(_dataset(shares_outstanding=None), _obs(scenarios=(Scenario(name="s", fcf_growth=0.0, years=2),)))
    (s,) = e.scenarios
    assert s.value_per_share is None and "share count" in s.reason
