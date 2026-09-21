"""Backtest entity pinning: a universe member whose ticker the SEC registry
has re-pointed at a successor filer must fetch the ORIGINAL entity's history.

Regression for the 2026 XOM regeneration: the registry mapped XOM to
ExxonMobil Holdings Corp (new CIK, FY2026 facts only), so every 2021-2025
as-of silently became `skip_no_pit_data` — 17 rows lost with no warning.
"""

from __future__ import annotations

from app.services.backtesting.events import fetch_entity_events
from app.services.backtesting.runner import fetch_member_facts
from app.services.backtesting.universe import UNIVERSE, UniverseMember


class _FakeSec:
    def __init__(self):
        self.calls: list[tuple] = []

    def resolve_cik(self, ticker):
        self.calls.append(("resolve", ticker))
        return 2115436  # the successor entity the registry now returns

    def company_facts(self, ticker):
        self.calls.append(("facts_by_ticker", ticker))
        return {"entity": "successor"}

    def company_facts_by_cik(self, cik):
        self.calls.append(("facts_by_cik", cik))
        return {"entity": "pinned"}

    def submissions_by_cik(self, cik):
        self.calls.append(("subs_by_cik", cik))
        return {"filings": {"recent": {}}, "sic": "2911"}

    def _cached_json(self, name, url):
        self.calls.append(("json", name, url))
        return {"filings": {"recent": {}}, "sic": "2911"}


def test_xom_is_pinned_to_the_pre_reorg_entity():
    xom = next(m for m in UNIVERSE if m.ticker == "XOM")
    assert xom.cik == 34088


def test_member_facts_honor_the_pin():
    sec = _FakeSec()
    facts = fetch_member_facts(sec, UniverseMember("XOM", "energy", "Energy", cik=34088))
    assert facts == {"entity": "pinned"}
    assert sec.calls == [("facts_by_cik", 34088)]


def test_member_facts_default_to_ticker_lookup():
    sec = _FakeSec()
    fetch_member_facts(sec, UniverseMember("AAPL", "control_tech", "Technology"))
    assert sec.calls == [("facts_by_ticker", "AAPL")]


def test_entity_events_pin_uses_the_pinned_cik():
    sec = _FakeSec()
    ev = fetch_entity_events(sec, "XOM", cik=34088)
    # No registry lookup: the pinned entity is fetched directly, so its
    # submissions never alias whatever the ticker now resolves to.
    assert ("resolve", "XOM") not in sec.calls
    assert sec.calls[0] == ("subs_by_cik", 34088)
    assert ev.sic == 2911


def test_entity_events_without_pin_resolve_via_registry():
    sec = _FakeSec()
    fetch_entity_events(sec, "XOM")
    # Both branches now go through the CIK accessor, so the cache entry is a
    # function of the entity actually fetched rather than of the ticker that
    # named it — a ticker reassigned to another filer cannot be served the
    # previous entity's index out of a ticker-named entry.
    assert sec.calls == [("resolve", "XOM"), ("subs_by_cik", 2115436)]


def test_entity_events_reuse_a_supplied_index_without_fetching():
    sec = _FakeSec()
    ev = fetch_entity_events(sec, "XOM", submissions={"filings": {"recent": {}}, "sic": "2911"})
    assert sec.calls == []
    assert ev.sic == 2911
