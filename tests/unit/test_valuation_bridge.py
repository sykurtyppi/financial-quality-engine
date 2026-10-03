"""The enterprise-value bridge: filing facts, one market observation and the
arithmetic between them, each line saying which it is. Built on the three
real fixtures and on a synthetic dataset without provenance.

The facts are read AS FILED by the observation (review of 48b1f04, F1): the
plane maps the raw companyfacts payload through the mapper's own
point-in-time cut (`build_dataset(as_of=)`, the `pit.py` path), so a period
re-filed later as a comparative or an amendment is seen as it stood on the
observation's day. Days are EDGAR's (US/Eastern): a filing dated the
observation's own Eastern day is not yet available (F5).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.schemas.financials import CompanyDataset
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.valuation.bridge import (
    available_through,
    enterprise_value_bridge,
    select_period,
)
from app.services.valuation.observation import LoadedObservation, MarketObservation
from app.services.valuation.plane import compute_plane
from tests.fixtures.companies import stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
ET = ZoneInfo("America/New_York")
RECORDED = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
DAY = date(2026, 10, 3)
# The FY2025 10-K of each filer (its filing date in the fixture): the period
# the live dataset carries with the LATER comparative filing's date.
TEN_K = {"KO": ("FY2025Q4", date(2026, 2, 20)), "CRM": ("FY2026Q4", date(2026, 3, 2)),
         "AAPL": ("FY2025Q4", date(2025, 10, 31))}


def _facts(ticker: str) -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


def _dataset(ticker: str):
    ds, _ = build_dataset(_facts(ticker), ticker)
    return ds


def _obs(ticker: str, price: float, observed_at: datetime = OBSERVED) -> MarketObservation:
    return MarketObservation(ticker=ticker, price=price, currency="USD", observed_at=observed_at,
                             source="test: synthetic observation",
                             recorded_at=max(RECORDED, observed_at))


def _afternoon(day: date) -> datetime:
    """16:00 Eastern on `day`: an observation of that Eastern calendar day."""
    return datetime.combine(day, datetime.min.time()).replace(hour=16, tzinfo=ET)


def _filed_days(facts: dict) -> list[date]:
    return sorted({date.fromisoformat(e["filed"]) for tax in facts["facts"].values()
                   for tag in tax.values() for rows in tag["units"].values()
                   for e in rows if e.get("filed")})


def _plane(ticker: str, observed_at: datetime, facts: dict | None = None, price: float = 100.0):
    facts = facts if facts is not None else _facts(ticker)
    ds, _ = build_dataset(facts, ticker)
    return compute_plane(ds, LoadedObservation.of(_obs(ticker, price, observed_at)), DAY,
                         company_facts=facts)


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
def test_filing_components_cite_exactly_the_periods_sources(ticker):
    ds = _dataset(ticker)
    b = enterprise_value_bridge(ds, _obs(ticker, 100.0))
    period = next(p for p in ds.periods if p.fiscal_label == b.fiscal_label)
    assert b.period_end == period.period_end
    filing = b.filing_components()
    assert filing, "no filing-derived line on a real fixture"
    for comp in filing:
        assert comp.basis == "filing"
        assert comp.sources == (period.sources[comp.name],)
        assert comp.value == getattr(period, comp.name)
    assert b.price.basis == "observation" and b.price.value == 100.0 and b.price.sources == ()
    assert b.shares.basis == "filing"
    assert b.market_cap.basis == "derived"
    assert b.market_cap.value == 100.0 * b.shares.value
    # The cover-page count carries its cover date, not the quarter end.
    cover = period.sources["shares_outstanding"].inputs[0].end
    assert b.shares.label == f"cover-page count dated {cover}"
    # EV reconciles over the lines, with STI / minority interest / preferred
    # read as 0 where the filer reported none — and said so.
    sti, mi, pref = (c.value or 0.0 for c in (b.short_term_investments, b.minority_interest,
                                              b.preferred_stock))
    assert b.ev == pytest.approx(b.market_cap.value + b.debt.value - b.cash.value - sti + mi + pref)
    assert b.ev_reason is None
    for c in (b.short_term_investments, b.minority_interest, b.preferred_stock):
        assert c.basis in ("filing", "assumption")
        if c.basis == "assumption":
            assert c.value == 0.0 and "not reported (assumed 0)" in (c.note or "")
            assert c.sources == ()
    # Operating leases are a line of their own and never in EV.
    assert b.operating_leases.in_ev is False and "not in EV" in b.operating_leases.label
    # A bridge over a dataset alone cannot check availability and says so.
    assert b.availability.startswith("filing availability at the observation date not checked "
                                     "(no raw facts)")
    assert b.fiscal_label == ds.sorted_periods()[-1].fiscal_label


# --- availability: the facts as filed by the observation (F1) ----------------------


def test_available_through_is_the_day_before_the_observations_eastern_day():
    # 23:30 Eastern on the 29th is already the 30th in UTC; the Eastern day
    # rules, and filings dated that day are not yet available.
    at = datetime(2026, 4, 29, 23, 30, tzinfo=ET)
    assert at.astimezone(UTC).date() == date(2026, 4, 30)
    assert available_through(at) == date(2026, 4, 28)
    # 00:30 UTC on the 30th is 20:30 Eastern on the 29th: the same answer.
    assert available_through(datetime(2026, 4, 30, 0, 30, tzinfo=UTC)) == date(2026, 4, 28)
    assert available_through(datetime(2026, 4, 30, 16, 0, tzinfo=ET)) == date(2026, 4, 29)


@pytest.mark.parametrize("ticker", ["KO", "CRM", "AAPL"])
def test_an_observation_after_the_10k_reads_the_10k_not_the_later_comparative(ticker):
    """The reviewer's case: the live dataset dates an FY-end period by the
    later 10-Q that repeated it, so the old bridge skipped the 10-K's period.
    As filed, the 10-K is what an observation the next day could see."""
    label, filed = TEN_K[ticker]
    plane = _plane(ticker, _afternoon(filed + timedelta(days=1)))
    b = plane.bridge
    assert plane.as_filed_by == filed
    assert b.fiscal_label == label
    refs = [r for c in b.filing_components() for sv in c.sources for r in sv.inputs]
    assert refs and max(r.filed for r in refs) <= filed
    assert {r.form for r in refs if r.filed == filed} <= {"10-K"}
    assert b.ev is not None
    assert b.availability.startswith(f"filing-derived facts as filed by {filed}")
    assert f"filings dated {filed + timedelta(days=1)} treated as not yet available" in b.availability
    # The live dataset carries the same period under the LATER filing's date.
    live = next(p for p in _dataset(ticker).periods if p.fiscal_label == label)
    live_filed = max(r.filed for name in ("total_debt", "cash_and_equivalents")
                     for r in live.sources[name].inputs)
    assert live_filed > filed


@pytest.mark.parametrize("ticker", ["KO", "CRM", "AAPL"])
def test_every_filing_day_yields_its_own_period_and_the_oldest_are_not_none(ticker):
    facts = _facts(ticker)
    days = _filed_days(facts)
    for day in days:
        plane = _plane(ticker, _afternoon(day + timedelta(days=1)), facts)
        try:
            pit, _ = build_dataset(facts, ticker, n_quarters=8, as_of=day)
        except ValueError:
            # Before the second quarter end is filed nothing can be mapped:
            # no EV, and the sentence names the cut.
            assert plane.bridge.fiscal_label is None and plane.bridge.ev is None
            assert plane.bridge.ev_reason.startswith("EV not asserted: no period can be built "
                                                     f"from the facts filed by {day}")
            continue
        # The latest period the cut maps that carries the bridge inputs.
        latest = pit.sorted_periods()[-1]
        assert plane.bridge.fiscal_label == latest.fiscal_label, day
        assert plane.bridge.ev is not None
        for c in plane.bridge.filing_components():
            assert c.value == getattr(latest, c.name)
            assert all(r.filed <= day for sv in c.sources for r in sv.inputs)
        assert plane.dataset is not None
        assert [p.fiscal_label for p in plane.dataset.periods] == [p.fiscal_label for p in pit.periods]


def test_an_observation_before_every_filing_asserts_no_ev():
    plane = _plane("KO", datetime(2020, 1, 1, 12, tzinfo=UTC))
    b = plane.bridge
    assert b.ev is None and b.fiscal_label is None and plane.dataset is None
    assert b.ev_reason.startswith("EV not asserted: no period can be built from the facts filed "
                                  "by 2019-12-31")
    assert "filings dated 2020-01-01 treated as not yet available" in b.ev_reason
    assert b.filing_components() == ()
    assert plane.ttm.label is None and "no period available" in plane.ttm.reason


def test_a_restated_figure_shows_the_as_filed_value_until_the_amendment_is_filed():
    """An amended cash figure for the newest KO quarter, filed after the
    observation, is not what the observation could see; after it, it is."""
    facts = _facts("KO")
    live = _dataset("KO")
    newest = live.sorted_periods()[-1]
    cash_ref = newest.sources["cash_and_equivalents"].inputs[0]
    tag = cash_ref.concept.split(":", 1)[1]
    rows = facts["facts"]["us-gaap"][tag]["units"]["USD"]
    original = next(e for e in rows if e["end"] == newest.period_end.isoformat()
                    and e["accn"] == cash_ref.accession)
    amended = {**original, "val": original["val"] + 1_000_000_000.0, "filed": "2026-06-01",
               "form": "10-Q/A", "accn": "0000021344-26-999999"}
    facts = copy.deepcopy(facts)
    facts["facts"]["us-gaap"][tag]["units"]["USD"] = [*rows, amended]
    # The live dataset now carries the amended value (latest-filed wins).
    ds, _ = build_dataset(facts, "KO")
    assert ds.sorted_periods()[-1].cash_and_equivalents == amended["val"]
    before = _plane("KO", _afternoon(date(2026, 5, 15)), facts).bridge
    assert before.fiscal_label == newest.fiscal_label
    assert before.cash.value == original["val"]
    assert [r.accession for sv in before.cash.sources for r in sv.inputs] == [cash_ref.accession]
    after = _plane("KO", _afternoon(date(2026, 6, 2)), facts).bridge
    assert after.cash.value == amended["val"]
    assert [r.accession for sv in after.cash.sources for r in sv.inputs] == [amended["accn"]]
    assert after.ev == pytest.approx(before.ev - 1_000_000_000.0)


def test_the_eastern_day_rules_not_the_utc_day():
    """KO's FY2026Q1 10-Q is dated 2026-04-30. Observed at 23:30 Eastern on
    the 29th (already the 30th in UTC) it is not available; at 22:30
    Eastern on the 30th, its own day, still not; the next Eastern day it is."""
    facts = _facts("KO")
    filed = date(2026, 4, 30)
    for at in (datetime(2026, 4, 29, 23, 30, tzinfo=ET), datetime(2026, 4, 30, 22, 30, tzinfo=ET)):
        b = _plane("KO", at, facts).bridge
        assert b.fiscal_label == "FY2025Q4", at
        assert all(r.filed < filed for c in b.filing_components() for sv in c.sources
                   for r in sv.inputs)
    b = _plane("KO", datetime(2026, 5, 1, 0, 30, tzinfo=ET), facts).bridge
    assert b.fiscal_label == "FY2026Q1"
    assert "as filed by 2026-04-30" in b.availability


@pytest.mark.parametrize("ticker", ["KO", "CRM", "AAPL"])
def test_at_or_after_the_newest_filing_the_as_filed_dataset_is_the_live_one(ticker):
    """What the drill rehearses: an observation of today reads exactly the
    dataset the report scored, period for period and source for source."""
    facts = _facts(ticker)
    live, _ = build_dataset(facts, ticker)
    plane = _plane(ticker, datetime.now(UTC) - timedelta(hours=1), facts)
    assert plane.dataset is not None
    assert plane.dataset.model_dump() == live.model_dump()
    assert [p.sources for p in plane.dataset.periods] == [p.sources for p in live.periods]
    assert plane.bridge.fiscal_label == live.sorted_periods()[-1].fiscal_label


def test_without_raw_facts_the_latest_period_is_used_and_the_check_is_said_not_made():
    ds = _dataset("KO")
    plane = compute_plane(ds, LoadedObservation.of(_obs("KO", 60.0)), DAY)
    assert plane.as_filed_by is None and plane.dataset is ds
    latest = ds.sorted_periods()[-1]
    assert plane.bridge.fiscal_label == latest.fiscal_label
    assert plane.bridge.availability == (
        "filing availability at the observation date not checked (no raw facts): latest "
        f"period {latest.fiscal_label} (ending {latest.period_end}) used")
    # The sentence never claims a check: no "as filed by".
    assert "as filed by" not in plane.bridge.availability


def test_select_period_prefers_the_latest_period_with_the_bridge_inputs():
    ds = _dataset("KO")
    periods = ds.sorted_periods()
    assert select_period(ds) == (periods[-1], [])
    periods[-1].total_debt = None
    period, skipped = select_period(ds)
    assert period is periods[-2] and skipped == [f"{periods[-1].fiscal_label} (total_debt missing)"]
    periods[-2].cash_and_equivalents = None
    period, skipped = select_period(ds)
    assert period is periods[-3]
    assert skipped == [f"{periods[-1].fiscal_label} (total_debt missing)",
                       f"{periods[-2].fiscal_label} (cash_and_equivalents missing)"]
    # Every period short of an input: the latest is used and EV not asserted.
    for p in periods:
        p.total_debt = None
    period, skipped = select_period(ds)
    assert period is periods[-1] and skipped == []
    b = enterprise_value_bridge(ds, _obs("KO", 60.0), as_filed_by=date(2026, 10, 1))
    assert b.ev is None and b.ev_reason == f"EV not asserted: total_debt missing for {periods[-1].fiscal_label}"
    assert select_period(CompanyDataset(profile=ds.profile, periods=[])) == (None, [])


def test_a_skipped_period_is_named_on_the_availability_line():
    ds = _dataset("KO")
    periods = ds.sorted_periods()
    periods[-1].shares_outstanding = None
    periods[-1].shares_diluted = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0), as_filed_by=date(2026, 10, 1))
    assert b.fiscal_label == periods[-2].fiscal_label
    assert b.availability.endswith(
        f"; skipped (bridge inputs missing): {periods[-1].fiscal_label} (share count missing)")


def test_select_period_without_provenance_takes_the_latest_and_says_so():
    ds = stretch_dataset()
    b = enterprise_value_bridge(ds, _obs("STRETCH", 10.0))
    period = ds.sorted_periods()[-1]
    assert b.fiscal_label == period.fiscal_label
    assert "not checked" in b.availability
    assert all(c.sources == () for c in b.components())
    assert b.shares.label == "cover-page count (cover date not recorded)"
    assert b.ev == pytest.approx(10.0 * period.shares_outstanding + period.total_debt
                                 - period.cash_and_equivalents)


def test_no_dataset_means_no_period_and_the_cut_is_named():
    obs = _obs("KO", 60.0, _afternoon(date(2026, 1, 2)))
    b = enterprise_value_bridge(None, obs, as_filed_by=date(2026, 1, 1))
    assert b.fiscal_label is None and b.period_end is None and b.ev is None
    assert b.availability == ("no period can be built from the facts filed by 2026-01-01 "
                              "(filings dated 2026-01-02 treated as not yet available)")
    assert b.ev_reason == f"EV not asserted: {b.availability}"
    assert all(c.value is None for c in b.components() if c.name != "price")
    assert b.filing_components() == ()


def test_missing_debt_means_ev_not_asserted_naming_the_field():
    ds = _dataset("KO")
    periods = ds.sorted_periods()
    for p in periods:
        p.total_debt = None
    period = periods[-1]
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.fiscal_label == period.fiscal_label
    assert b.ev is None
    assert b.ev_reason == f"EV not asserted: total_debt missing for {period.fiscal_label}"
    assert b.debt.value is None and b.debt.basis == "filing"
    assert b.market_cap.value is not None  # the lines that can be built still are


def test_a_latest_period_short_of_an_input_is_skipped_for_the_one_before():
    ds = _dataset("KO")
    periods = ds.sorted_periods()
    periods[-1].total_debt = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.fiscal_label == periods[-2].fiscal_label and b.ev is not None
    assert b.availability == (
        f"filing availability at the observation date not checked (no raw facts): "
        f"{periods[-2].fiscal_label} (ending {periods[-2].period_end}) used; skipped (bridge "
        f"inputs missing): {periods[-1].fiscal_label} (total_debt missing)")


def test_missing_cash_means_ev_not_asserted():
    ds = _dataset("KO")
    for p in ds.sorted_periods():
        p.cash_and_equivalents = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.ev is None and "cash_and_equivalents" in b.ev_reason


def test_share_count_falls_back_to_the_weighted_average_then_to_nothing():
    ds = _dataset("KO")
    periods = ds.sorted_periods()
    period = periods[-1]
    for p in periods:
        p.shares_outstanding = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.fiscal_label == period.fiscal_label
    assert b.shares.value == period.shares_diluted
    assert b.shares.label == f"weighted-average diluted, {period.fiscal_label}"
    assert b.shares.sources == (period.sources["shares_diluted"],)
    assert b.ev is not None
    for p in periods:
        p.shares_diluted = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.ev is None and b.market_cap.value is None
    assert "share count" in b.ev_reason and period.fiscal_label in b.ev_reason


def test_optional_components_are_read_when_reported():
    ds = stretch_dataset()
    period = ds.sorted_periods()[-1]
    period.short_term_investments = 50.0
    period.minority_interest = 20.0
    period.preferred_stock = 5.0
    period.operating_lease_liabilities = 300.0
    period.stockholders_equity = 900.0
    b = enterprise_value_bridge(ds, _obs("STRETCH", 10.0))
    assert b.short_term_investments.basis == "filing" and b.short_term_investments.value == 50.0
    assert b.ev == pytest.approx(10.0 * period.shares_outstanding + period.total_debt
                                 - period.cash_and_equivalents - 50.0 + 20.0 + 5.0)
    assert b.operating_leases.value == 300.0 and b.operating_leases.in_ev is False
    assert b.equity.value == 900.0 and b.equity.in_ev is False
    # Signs are what the arithmetic used.
    assert [c.sign for c in (b.market_cap, b.debt, b.cash, b.short_term_investments,
                             b.minority_interest, b.preferred_stock)] == [1, 1, -1, -1, 1, 1]
