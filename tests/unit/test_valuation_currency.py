"""The valuation plane refuses a price in another currency than the filing's.

Hermes audit of PR #118 @ 3983f8a (finding 2, blocker): the mapper reads
every monetary fact in USD (`companyfacts_mapper._collect(..., "USD")`), the
price box accepted any three-letter currency, and the bridge computed
``market_cap = price × shares`` and added USD debt and cash to it: a price
in EUR made a market cap, an EV, eight multiples, two implied growths and
every scenario of mixed units, each printed as a number.

There is no FX conversion in the engine and none is guessed: a price whose
currency is not the filing figures' (`fields.FILING_CURRENCY`) asserts no
market cap, EV, multiple, implied growth or scenario, each line saying why.
The filing facts themselves are still shown (they are facts), the
observation is shown as recorded, and nothing that scores changes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.schemas.ledger import Plane
from app.services.ingestion import fields
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.reporting.ledger import build_ledger
from app.services.valuation import bridge as bridge_mod
from app.services.valuation.observation import (
    Assumptions,
    LoadedObservation,
    MarketObservation,
    Scenario,
)
from app.services.valuation.plane import compute_plane
from app.services.valuation.render import render_valuation_section

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DAY = date(2026, 10, 3)
BUILT = datetime(2026, 10, 3, 9, tzinfo=UTC)
REASON = "price in EUR, filing figures in USD — no FX conversion"


def _facts(ticker: str = "KO") -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


def _obs(currency: str, **kw) -> MarketObservation:
    return MarketObservation(
        ticker="KO", price=61.0, currency=currency,
        observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC), source="Xetra close (test)",
        recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC),
        scenarios=(Scenario(name="base", fcf_growth=0.04, years=5),
                   Scenario(name="bear", fcf_growth=-0.02, years=5, required_return=0.1)),
        **kw)


def _plane(currency: str, *, raw: bool = True, **kw):
    facts = _facts()
    ds, _ = build_dataset(facts, "KO")
    return compute_plane(ds, LoadedObservation.of(_obs(currency, **kw)), DAY,
                         company_facts=facts if raw else None, now=BUILT)


def test_the_filing_currency_is_one_constant_the_mapper_and_the_bridge_share():
    assert fields.FILING_CURRENCY == "USD"
    assert fields.unit_for("not_a_field") == fields.FILING_CURRENCY
    assert fields.field("revenue").unit == fields.FILING_CURRENCY
    assert bridge_mod.FILING_CURRENCY is fields.FILING_CURRENCY


@pytest.mark.parametrize("raw", [True, False], ids=["as-filed", "dataset-only"])
def test_a_price_in_eur_asserts_no_market_cap_ev_multiple_growth_or_scenario(raw):
    plane = _plane("EUR", raw=raw)
    b = plane.bridge
    # The facts are still the facts; only what mixes the two units goes.
    assert b.price.value == 61.0 and "61.00 EUR" in b.price.label
    assert b.shares.value is not None and b.debt.value is not None and b.cash.value is not None
    assert b.market_cap.value is None and REASON in b.market_cap.note
    assert b.ev is None and b.ev_reason == f"EV not asserted: {REASON}"
    assert len(plane.multiples) == 8
    for m in plane.multiples:
        assert m.value is None and REASON in m.reason, m
    e = plane.expectations
    for g in (e.gordon, e.reverse):
        assert g.value is None and REASON in g.reason, g
    assert e.sensitivities == () and e.main_assumption is None
    assert len(e.scenarios) == 2
    for sc in e.scenarios:
        assert sc.value_per_share is None and sc.upside is None and REASON in sc.reason, sc


def test_the_reason_names_the_currency_recorded():
    plane = _plane("GBP")
    assert plane.bridge.ev_reason == ("EV not asserted: price in GBP, filing figures in USD "
                                      "— no FX conversion")


def test_a_usd_price_is_unchanged():
    plane = _plane("USD")
    assert plane.bridge.ev is not None and plane.bridge.market_cap.value is not None
    assert any(m.value is not None for m in plane.multiples)
    assert all(sc.value_per_share is not None for sc in plane.expectations.scenarios)


def test_the_check_is_against_the_constant_not_a_second_literal(monkeypatch):
    """Were the filing currency another, a price in it would be the one
    bridged: the bridge reads the constant, not a "USD" of its own."""
    monkeypatch.setattr(bridge_mod, "FILING_CURRENCY", "EUR")
    plane = _plane("EUR")
    assert plane.bridge.ev is not None
    assert _plane("USD").bridge.ev_reason == (
        "EV not asserted: price in USD, filing figures in EUR — no FX conversion")


def test_operator_assumptions_do_not_bring_the_growth_back():
    plane = _plane("EUR", assumptions=Assumptions(required_return=0.09))
    assert plane.expectations.gordon.value is None
    assert REASON in plane.expectations.gordon.reason


def test_the_card_says_the_reason_on_every_row_and_prints_no_number_for_them():
    s = render_valuation_section(_plane("EUR"))
    assert "- [O] price 61.00 EUR observed" in s
    row = next(line for line in s.splitlines() if line.startswith("| market cap"))
    assert "| — |" in row and REASON in row
    row = next(line for line in s.splitlines() if line.startswith("| **enterprise value**"))
    assert "| — |" in row and REASON in row
    multiples = [line for line in s.splitlines()
                 if re.match(r"\| (P/E|EV/EBIT|EV/EBITDA|EV/Sales|P/S|P/FCF|FCF yield|"
                             r"earnings yield) \|", line)]
    assert len(multiples) == 8
    for line in multiples:
        assert line.split("|")[2].strip() == "—" and REASON in line, line
    growth = [line for line in s.splitlines() if "implied" in line and line.startswith("- [D]")]
    assert len(growth) == 2 and all(REASON in line for line in growth)
    scen = [line for line in s.splitlines() if line.startswith("- [A] model assumption:")]
    assert len(scen) == 2 and all(REASON in line and "value per share" not in line
                                  for line in scen)


def test_the_ledger_records_the_refusal():
    facts = _facts()
    ds, _ = build_dataset(facts, "KO")
    plane = compute_plane(ds, LoadedObservation.of(_obs("EUR")), DAY, company_facts=facts,
                          now=BUILT)
    doc = build_ledger(result=analyze(ds), dataset=ds, ticker="KO", report_date=DAY,
                       cik_sources={"the companyfacts payload": 21344}, valuation=plane)
    assert doc.valuation is not None and doc.valuation.state == "produced"
    assert doc.valuation.ev is None and doc.valuation.ev_reason == f"EV not asserted: {REASON}"
    rows = [i for i in doc.items if i.plane is Plane.VALUATION]
    derived = [i for i in rows if i.kind in ("market_cap", "enterprise_value", "multiple",
                                             "implied_growth", "scenario")]
    assert len(derived) == 1 + 1 + 8 + 2 + 2
    for item in derived:
        assert item.value is None and REASON in item.claim, item
    price = next(i for i in rows if i.kind == "market_observation")
    assert "61.00 EUR" in price.claim
    assert any(i.kind == "bridge_component" and i.value is not None for i in rows)


# --- every monetary figure on the card says its currency -----------------------------


def test_every_monetary_figure_on_the_card_carries_its_currency():
    s = render_valuation_section(_plane("USD"))
    lines = s.splitlines()
    assert "- [O] price 61.00 USD observed" in s
    table = [line for line in lines if re.match(r"\| .* \| [FOAD] \| ", line)]
    money = [line for line in table if not line.startswith(("| cover-page", "| balance-sheet",
                                                           "| weighted-average"))]
    assert len(money) == 10  # price, market cap, debt, cash, three assumed-or-read, EV, leases, equity
    for line in money:
        value = line.split("|")[3].strip()
        assert value == "—" or value.endswith(" USD"), line
    shares = next(line for line in table if line.startswith(("| cover-page", "| balance-sheet")))
    assert not shares.split("|")[3].strip().endswith("USD")  # a count, not money
    ttm = next(line for line in lines if line.startswith("- [F] TTM figures ("))
    assert len(re.findall(r"-?[\d,]+ USD", ttm)) == 5
    for line in lines:
        if re.match(r"\| (P/E|EV/|P/S|P/FCF|FCF yield|earnings yield)", line) and "x |" in line:
            cells = [c.strip() for c in line.split("|")[3:5]]
            assert all(c.endswith(" USD") for c in cells), line
    scen = [line for line in lines if "value per share" in line]
    assert scen and all(re.search(r"value per share [\d,.]+ USD vs price 61\.00 USD", line)
                        for line in scen)


# --- fix round 4 (Hermes re-audit of #118 @ 34836cf) -----------------------------------
# A finite price can still overflow: 1e308 × the share count is inf, which
# the ledger wrote as null with no reason. Every derived value is checked
# finite, and units are stated in the ledger.


def _synthetic(**overrides):
    from app.schemas.financials import (
        CompanyDataset,
        CompanyProfile,
        PeriodFinancials,
        PeriodType,
    )

    ends = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))
    base = dict(revenue=1000.0, net_income=100.0, ebit=150.0, depreciation_amortization=50.0,
                cfo=200.0, capex=50.0, cash_and_equivalents=100.0, total_debt=300.0,
                shares_outstanding=10.0)
    base.update(overrides)
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER,
                                fiscal_label=f"FY2025Q{i + 1}", **base) for i, e in enumerate(ends)]
    return CompanyDataset(profile=CompanyProfile(ticker="KO"), periods=periods)


def _overflow_plane(ds, price):
    obs = MarketObservation(ticker="KO", price=price, currency="USD",
                            observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC),
                            source="test", recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC),
                            scenarios=(Scenario(name="base", fcf_growth=0.04, years=5),))
    return compute_plane(ds, LoadedObservation.of(obs), DAY, now=BUILT)


def _finite_or_said(plane):
    import math

    b = plane.bridge
    values = [(b.market_cap.value, b.market_cap.note), (b.ev, b.ev_reason)]
    values += [(m.value, m.reason) for m in plane.multiples]
    e = plane.expectations
    values += [(g.value, g.reason) for g in (e.gordon, e.reverse)]
    values += [(sc.value_per_share, sc.reason) for sc in e.scenarios]
    values += [(sc.upside, sc.reason) for sc in e.scenarios]
    for value, reason in values:
        assert value is None or math.isfinite(value), (value, reason)
        if value is None:
            assert reason, "no number and no reason"
    return values


def test_a_price_whose_market_cap_overflows_asserts_nothing_and_says_why():
    plane = _overflow_plane(build_dataset(_facts(), "KO")[0], 1e308)
    _finite_or_said(plane)
    b = plane.bridge
    assert b.market_cap.value is None
    assert b.market_cap.note == "market cap not computable: overflow (price × shares)"
    assert b.ev is None and "overflow (price × shares)" in b.ev_reason
    assert all(m.value is None and "overflow" in m.reason for m in plane.multiples)
    e = plane.expectations
    assert e.gordon.value is None and "overflow" in e.gordon.reason
    assert e.reverse.value is None and "overflow" in e.reverse.reason
    # A scenario values FCF per share, not the price: finite here (against
    # the price it is -100%), and said if it ever is not (below).


def test_an_ev_a_multiple_a_growth_or_a_scenario_that_overflows_says_so():
    # EV: a finite market cap plus a debt near the largest double.
    plane = _overflow_plane(_synthetic(total_debt=1.7e308), 1.7e307)
    _finite_or_said(plane)
    assert plane.bridge.market_cap.value is not None and plane.bridge.ev is None
    assert plane.bridge.ev_reason.startswith("EV not computable: overflow")
    # A multiple: a revenue so small the ratio is past a double.
    plane = _overflow_plane(_synthetic(revenue=1e-310), 61.0)
    _finite_or_said(plane)
    ps = next(m for m in plane.multiples if m.name == "P/S")
    assert ps.value is None and ps.reason == "not computable: overflow (P/S)"
    # The implied growth and a scenario: a share count so small that FCF over
    # the market cap, and the value per share, are past a double.
    plane = _overflow_plane(_synthetic(shares_outstanding=1e-300), 1e-10)
    _finite_or_said(plane)
    e = plane.expectations
    assert e.gordon.value is None and "overflow" in e.gordon.reason
    assert e.scenarios[0].value_per_share is None and "overflow" in e.scenarios[0].reason


def test_an_overflowing_price_round_trips_through_the_ledger_with_its_reasons():
    from app.schemas.ledger import LedgerDocument

    ds, _ = build_dataset(_facts(), "KO")
    plane = _overflow_plane(ds, 1e308)
    doc = build_ledger(result=analyze(ds), dataset=ds, ticker="KO", report_date=DAY,
                       cik_sources={"the companyfacts payload": 21344}, valuation=plane)
    text = doc.model_dump_json()
    assert "Infinity" not in text and "NaN" not in text
    assert LedgerDocument.model_validate_json(text) == doc
    for item in doc.items:
        if item.plane is Plane.VALUATION and item.value is None:
            assert "not computable" in item.claim or "not asserted" in item.claim, item


def test_the_ledger_states_the_currency_and_the_unit_of_every_monetary_figure():
    from app.schemas.ledger import LedgerDocument, ValuationSummary

    facts = _facts()
    ds, _ = build_dataset(facts, "KO")
    for currency in ("USD", "EUR"):
        plane = compute_plane(ds, LoadedObservation.of(_obs(currency)), DAY, company_facts=facts,
                              now=BUILT)
        doc = build_ledger(result=analyze(ds), dataset=ds, ticker="KO", report_date=DAY,
                           cik_sources={"the companyfacts payload": 21344}, valuation=plane)
        assert doc.valuation.currency == currency
        rows = {(i.kind, i.subject): i for i in doc.items if i.plane is Plane.VALUATION}
        assert rows[("market_observation", "price")].currency == currency
        assert rows[("bridge_component", "total_debt")].currency == "USD"
        assert rows[("bridge_component", "cash_and_equivalents")].currency == "USD"
        assert rows[("bridge_component", "shares_outstanding")].currency is None  # a count
        assert rows[("market_cap", "market_cap")].currency == "USD"
        assert rows[("enterprise_value", "enterprise_value")].currency == "USD"
        assert rows[("ttm_figure", "revenue")].currency == "USD"
        assert all(i.currency is None for (k, _), i in rows.items()
                   if k in ("multiple", "implied_growth"))
        assert all(i.currency == "USD" for (k, _), i in rows.items() if k == "scenario")
        debt = rows[("bridge_component", "total_debt")]
        assert debt.provenance and {p.unit for p in debt.provenance} == {"USD"}
        shares = rows[("bridge_component", "shares_outstanding")]
        assert {p.unit for p in shares.provenance} == {"shares"}
        assert LedgerDocument.model_validate_json(doc.model_dump_json()) == doc
    # Old ledgers, without either field, load.
    old = ValuationSummary.model_validate({"state": "produced"})
    assert old.currency is None
