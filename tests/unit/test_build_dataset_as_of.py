"""One point-in-time path: `build_dataset(as_of=)` is the dataset a reader
could have built on that day, and equals the old composition exactly.

Every PIT caller used to filter the payload (`pit.filter_as_of`) and then
map it. The mapper now takes the date itself, so replay and the ledger can
ask the one resolver directly. `filter_as_of` stays as the reference: the
metamorphic equality below — dataset, per-value sources and diagnostics,
exact — is the proof that moving the cut inside the mapper moved nothing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.backtesting.pit import build_pit_dataset, filter_as_of
from app.services.ingestion.companyfacts_mapper import build_dataset
from tests.strategies import AS_OF, companyfacts

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
TICKERS = ["AAPL", "KO", "CRM"]


def _mapped(facts: dict, ticker: str, **kw):
    """Everything the mapper produces, or the refusal it produces instead."""
    try:
        dataset, diag = build_dataset(facts, ticker, **kw)
    except ValueError as e:
        return ("refused", str(e))
    return (dataset.model_dump(), [p.sources for p in dataset.periods], diag.model_dump())


def _filed_days(facts: dict) -> list[date]:
    return sorted({
        date.fromisoformat(e["filed"])
        for concepts in facts["facts"].values() for concept in concepts.values()
        for rows in concept["units"].values() for e in rows if e.get("filed")
    })


def _real(ticker: str) -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


@pytest.mark.parametrize("ticker", TICKERS)
def test_the_mappers_cut_equals_filtering_first_on_the_real_fixtures(ticker):
    """At every day a filing landed, the day before it, and each quarter end
    the mapper reports: the two paths agree on every value, every source
    and every diagnostic — or refuse with the same message."""
    facts = _real(ticker)
    live, _ = build_dataset(facts, ticker)
    cuts = set()
    for day in _filed_days(facts):
        cuts |= {day, day - timedelta(days=1)}
    cuts |= {p.period_end for p in live.periods}
    for cut in sorted(cuts):
        assert _mapped(filter_as_of(facts, cut), ticker) == _mapped(facts, ticker, as_of=cut), cut


@settings(max_examples=150)
@given(facts=companyfacts(), cut=st.integers(-400, 60))
def test_the_mappers_cut_equals_filtering_first_on_generated_payloads(facts, cut):
    """Undated facts, same-day ties, amendments, composites and debt roles."""
    as_of = AS_OF + timedelta(days=cut)
    assert _mapped(filter_as_of(facts, as_of), "T") == _mapped(facts, "T", as_of=as_of)


def _payload(*rows: dict) -> dict:
    return {"entityName": "T", "facts": {"us-gaap": {
        "Assets": {"units": {"USD": [
            {"end": "2024-03-31", "val": 100.0, "filed": "2024-05-01", "form": "10-Q", "accn": "a"},
            {"end": "2024-06-30", "val": 110.0, "filed": "2024-08-01", "form": "10-Q", "accn": "b"},
            *rows,
        ]}},
    }}}


def _assets(facts: dict, **kw) -> dict:
    dataset, _ = build_dataset(facts, "T", n_quarters=2, **kw)
    return {p.period_end.isoformat(): p.total_assets for p in dataset.periods}


def test_a_fact_filed_on_the_day_is_visible_and_the_day_after_is_not():
    amended = {"end": "2024-06-30", "val": 999.0, "filed": "2024-09-15", "form": "10-Q/A",
               "accn": "c"}
    facts = _payload(amended)
    assert _assets(facts, as_of=date(2024, 9, 15))["2024-06-30"] == 999.0
    assert _assets(facts, as_of=date(2024, 9, 14))["2024-06-30"] == 110.0


def test_an_undated_fact_is_invisible_to_a_dated_reader_only():
    undated = {"end": "2024-06-30", "val": 555.0, "form": "10-Q", "accn": "u"}
    later = {"end": "2024-06-30", "val": 555.0, "filed": "garbled", "form": "10-Q", "accn": "g"}
    for extra in (undated, later):
        facts = _payload({"end": "2024-03-31", "val": 1.0, "filed": "2024-02-01",
                          "form": "10-Q", "accn": "old"}, extra)
        assert _assets(facts, as_of=date(2030, 1, 1))["2024-06-30"] == 110.0
    # Live (no date), the undated fact still reads as the oldest, as always.
    assert _assets(_payload(undated))["2024-06-30"] == 110.0


def test_a_dated_dataset_names_only_filings_made_by_then():
    for ticker in TICKERS:
        facts = _real(ticker)
        days = _filed_days(facts)
        cut = days[len(days) * 2 // 3]
        dataset, _ = build_dataset(facts, ticker, as_of=cut)
        refs = [r for p in dataset.periods for sv in p.sources.values() for r in sv.inputs]
        assert refs and max(r.filed for r in refs) <= cut


def test_the_quarter_ends_and_calendar_are_cut_too():
    """A quarter that only a later filing reports does not exist yet: the
    cut applies before the quarter ends are chosen, not after."""
    facts = _payload({"end": "2024-09-30", "val": 120.0, "filed": "2024-11-01",
                      "form": "10-Q", "accn": "d"})
    before, _ = build_dataset(facts, "T", n_quarters=4, as_of=date(2024, 10, 1))
    assert [p.period_end.isoformat() for p in before.periods] == ["2024-03-31", "2024-06-30"]


@pytest.mark.parametrize("ticker", TICKERS)
def test_the_backtest_wrapper_is_the_old_composition(ticker):
    facts = _real(ticker)
    cut = _filed_days(facts)[-5]
    new_ds, new_diag = build_pit_dataset(facts, ticker, cut)
    old_ds, old_diag = build_dataset(filter_as_of(facts, cut), ticker)
    assert new_ds.model_dump() == old_ds.model_dump()
    assert [p.sources for p in new_ds.periods] == [p.sources for p in old_ds.periods]
    assert new_diag == old_diag
    # …and it is a cut: the latest filing is not in it.
    live, _ = build_dataset(facts, ticker)
    assert new_ds.model_dump() != live.model_dump()


def test_a_share_count_filed_under_usd_is_still_found_when_its_shares_unit_is_not_yet_filed():
    """Some filers put share counts under USD; the mapper falls back to that
    unit when a concept has no `shares` unit. On a day when every `shares`
    entry is still in the future, the reader of that day saw no `shares`
    unit at all, so the fallback must apply — an emptied unit is not a unit."""
    facts = _payload()
    facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {"units": {
        "shares": [{"start": "2024-04-01", "end": "2024-06-30", "val": 7.0,
                    "filed": "2024-10-01", "form": "10-Q/A", "accn": "s"}],
        "USD": [{"start": "2024-04-01", "end": "2024-06-30", "val": 5.0,
                 "filed": "2024-08-01", "form": "10-Q", "accn": "u"}],
    }}
    cut = date(2024, 9, 1)
    dataset, _ = build_dataset(facts, "T", n_quarters=2, as_of=cut)
    assert dataset.periods[-1].shares_diluted == 5.0
    assert _mapped(facts, "T", n_quarters=2, as_of=cut) == _mapped(
        filter_as_of(facts, cut), "T", n_quarters=2
    )
