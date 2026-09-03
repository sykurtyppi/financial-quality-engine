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


def test_entity_events_pin_uses_cik_url_and_cik_cache_key():
    sec = _FakeSec()
    ev = fetch_entity_events(sec, "XOM", cik=34088)
    # No registry lookup, CIK-keyed cache (never aliasing the ticker's current
    # entity), CIK in the URL.
    assert ("resolve", "XOM") not in sec.calls
    kind, name, url = sec.calls[0]
    assert name == "submissions_CIK0000034088.json"
    assert url.endswith("CIK0000034088.json")
    assert ev.sic == 2911


def test_entity_events_without_pin_resolve_via_registry():
    sec = _FakeSec()
    fetch_entity_events(sec, "XOM")
    assert sec.calls[0] == ("resolve", "XOM")
    assert sec.calls[1][1] == "submissions_XOM.json"
