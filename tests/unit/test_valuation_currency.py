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
