"""The enterprise-value bridge: filing facts, one market observation and the
arithmetic between them, each line saying which it is. Built on the three
real fixtures and on a synthetic dataset without provenance."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    FactRef,
    PeriodFinancials,
    PeriodType,
    SourcedValue,
)
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.valuation.bridge import (
    Bridge,
    enterprise_value_bridge,
    select_period,
)
from app.services.valuation.observation import MarketObservation
from tests.fixtures.companies import stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
RECORDED = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)


def _dataset(ticker: str):
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    ds, _ = build_dataset(facts, ticker)
    return ds


def _obs(ticker: str, price: float, observed_at: datetime = OBSERVED) -> MarketObservation:
    return MarketObservation(ticker=ticker, price=price, currency="USD", observed_at=observed_at,
                             source="test: synthetic observation", recorded_at=RECORDED)


def _bridge_filed(period: PeriodFinancials) -> datetime:
    """The latest filing date among the facts behind the period's bridge fields."""
    return max(ref.filed for name, sv in period.sources.items()
               if name in Bridge.FILING_FIELDS for ref in sv.inputs)


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
    assert b.availability.startswith("filing-derived facts filed by ")


def test_an_observation_before_the_newest_filing_uses_the_period_filed_by_then():
    ds = _dataset("KO")
    newest = ds.sorted_periods()[-1]
    filed = _bridge_filed(newest)
    earlier = datetime.combine(filed - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    b = enterprise_value_bridge(ds, _obs("KO", 60.0, observed_at=earlier))
    assert b.fiscal_label != newest.fiscal_label
    chosen = next(p for p in ds.periods if p.fiscal_label == b.fiscal_label)
    assert _bridge_filed(chosen).isoformat() <= earlier.date().isoformat()
    assert f"filed by {_bridge_filed(chosen)}" in b.availability
    assert newest.fiscal_label in b.availability  # the skipped period is named
    # Observed after everything: the newest period — and on the filing day
    # itself (filed on or before the observation's day counts).
    assert enterprise_value_bridge(ds, _obs("KO", 60.0)).fiscal_label == newest.fiscal_label
    on_the_day = datetime.combine(filed, datetime.min.time(), tzinfo=UTC)
    assert enterprise_value_bridge(ds, _obs("KO", 60.0, observed_at=on_the_day)).fiscal_label == (
        newest.fiscal_label)


def _sourced(field: str, value: float, end: date, filed: date, concept: str = "us-gaap:X") -> SourcedValue:
    ref = FactRef(concept=concept, accession="0000000001-26-000001", filed=filed, form="10-Q",
                  start=None, end=end, value=value)
    return SourcedValue(field=field, value=value, strategy="single", method="direct", inputs=(ref,))


def _synthetic(sources_by_period: list[dict[str, SourcedValue]]) -> CompanyDataset:
    """Two quarters, each with the given per-value provenance."""
    ends = (date(2026, 3, 31), date(2026, 6, 30))
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER, fiscal_label=f"FY2026Q{i + 1}",
                                shares_outstanding=10.0, total_debt=30.0, cash_and_equivalents=5.0,
                                revenue=100.0, sources=src)
               for i, (e, src) in enumerate(zip(ends, sources_by_period))]
    return CompanyDataset(profile=CompanyProfile(ticker="SYN"), periods=periods)


def test_availability_reads_the_bridge_fields_filings_not_a_later_flow_filing():
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    ds = _synthetic([
        {"total_debt": _sourced("total_debt", 30.0, q1, date(2026, 5, 1))},
        {"total_debt": _sourced("total_debt", 30.0, q2, date(2026, 8, 1)),
         # revenue re-filed later (a comparative): not a bridge field
         "revenue": _sourced("revenue", 100.0, q2, date(2026, 11, 1))},
    ])
    b = enterprise_value_bridge(ds, _obs("SYN", 1.0, observed_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert b.fiscal_label == "FY2026Q2" and "filed by 2026-08-01" in b.availability


def test_a_period_whose_provenance_names_no_bridge_field_is_dated_by_its_other_facts():
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    ds = _synthetic([
        {"total_debt": _sourced("total_debt", 30.0, q1, date(2026, 5, 1))},
        {"revenue": _sourced("revenue", 100.0, q2, date(2026, 11, 1))},  # only a flow is sourced
    ])
    b = enterprise_value_bridge(ds, _obs("SYN", 1.0, observed_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert b.fiscal_label == "FY2026Q1" and "FY2026Q2 (filed 2026-11-01)" in b.availability


def test_a_period_with_no_dated_fact_is_skipped_and_named():
    q1 = date(2026, 3, 31)
    undated = SourcedValue(field="revenue", value=100.0, strategy="single", method="direct", inputs=())
    ds = _synthetic([
        {"total_debt": _sourced("total_debt", 30.0, q1, date(2026, 5, 1))},
        {"revenue": undated},
    ])
    b = enterprise_value_bridge(ds, _obs("SYN", 1.0, observed_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert b.fiscal_label == "FY2026Q1" and "FY2026Q2 (no dated fact)" in b.availability


def test_an_observation_before_every_filing_asserts_no_ev():
    ds = _dataset("KO")
    oldest = min(_bridge_filed(p) for p in ds.periods if p.sources)
    before = datetime.combine(oldest - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    b = enterprise_value_bridge(ds, _obs("KO", 60.0, observed_at=before))
    assert b.ev is None and b.fiscal_label is None
    assert "no period" in b.ev_reason and before.date().isoformat() in b.ev_reason
    assert b.filing_components() == ()


def test_select_period_without_provenance_takes_the_latest_and_says_so():
    ds = stretch_dataset()
    period, availability = select_period(ds, OBSERVED)
    assert period is ds.sorted_periods()[-1]
    assert "not checked" in availability
    b = enterprise_value_bridge(ds, _obs("STRETCH", 10.0))
    assert b.fiscal_label == period.fiscal_label
    assert all(c.sources == () for c in b.components())
    assert b.shares.label == "cover-page count (cover date not recorded)"
    assert b.ev == pytest.approx(10.0 * period.shares_outstanding + period.total_debt
                                 - period.cash_and_equivalents)


def test_missing_debt_means_ev_not_asserted_naming_the_field():
    ds = _dataset("KO")
    period = ds.sorted_periods()[-1]
    period.total_debt = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.ev is None
    assert b.ev_reason == f"EV not asserted: total_debt missing for {period.fiscal_label}"
    assert b.debt.value is None and b.debt.basis == "filing"
    assert b.market_cap.value is not None  # the lines that can be built still are


def test_missing_cash_means_ev_not_asserted():
    ds = _dataset("KO")
    period = ds.sorted_periods()[-1]
    period.cash_and_equivalents = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.ev is None and "cash_and_equivalents" in b.ev_reason


def test_share_count_falls_back_to_the_weighted_average_then_to_nothing():
    ds = _dataset("KO")
    period = ds.sorted_periods()[-1]
    period.shares_outstanding = None
    b = enterprise_value_bridge(ds, _obs("KO", 60.0))
    assert b.shares.value == period.shares_diluted
    assert b.shares.label == f"weighted-average diluted, {period.fiscal_label}"
    assert b.shares.sources == (period.sources["shares_diluted"],)
    assert b.ev is not None
    period.shares_diluted = None
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
