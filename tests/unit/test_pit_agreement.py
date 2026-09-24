"""PR 0.6 — the three point-in-time and same-day tie rules must agree.

`pit.filter_as_of` (backtests, controls, brief/derived.py), the restatement
detector's `_eligible_rows` (the report's evidence) and the vintage store's
`_take` each decide independently which facts a report dated `as_of` was
entitled to see and which same-day duplicate is "the" value. The detector's
rule was pinned in PR #37; `filter_as_of`'s was not — flipping `<=` to `<`
passed the suite. These tests pin the boundary and the pairwise agreement
on generated filing trails (hypothesis), including undated facts.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion import restatements as rs
from app.services.ingestion import vintages as vs
from app.services.ingestion.companyfacts_mapper import (
    _collect,
    _dedupe_latest_filed,
)
from tests.strategies import fact_rows, same_day_trails

AS_OF = date(2025, 8, 15)


def _fact(end, val, filed, *, form="10-Q", accn="A", start=None):
    d = {"end": end.isoformat(), "val": val, "form": form, "accn": accn, "fy": end.year, "fp": "Q2"}
    if filed is not None:
        d["filed"] = filed.isoformat()
    if start is not None:
        d["start"] = start.isoformat()
    return d


def _facts(rows, tag="Assets", unit="USD"):
    return {"entityName": "T", "facts": {"us-gaap": {tag: {"units": {unit: rows}}}}}


class TestFilterAsOfBoundary:
    def test_a_fact_filed_on_the_as_of_date_is_visible(self):
        rows = [_fact(date(2025, 6, 30), 1.0, AS_OF)]
        kept = filter_as_of(_facts(rows), AS_OF)["facts"]["us-gaap"]["Assets"]["units"]["USD"]
        assert [r["val"] for r in kept] == [1.0]

    def test_a_fact_filed_the_next_day_is_not(self):
        rows = [_fact(date(2025, 6, 30), 1.0, AS_OF + timedelta(days=1))]
        assert filter_as_of(_facts(rows), AS_OF)["facts"] == {}

    def test_an_undated_fact_is_dropped(self):
        rows = [_fact(date(2025, 6, 30), 1.0, None)]
        assert filter_as_of(_facts(rows), AS_OF)["facts"] == {}

    def test_same_boundary_in_the_detector(self):
        on = _eligible([_fact(date(2025, 6, 30), 1.0, AS_OF)])
        after = _eligible([_fact(date(2025, 6, 30), 1.0, AS_OF + timedelta(days=1))])
        undated = _eligible([_fact(date(2025, 6, 30), 1.0, None)])
        assert ([r["val"] for r in on], after, undated) == ([1.0], [], [])


def _eligible(rows):
    return rs._eligible_rows(_facts(rows), "us-gaap", "Assets", "USD", AS_OF)


@given(rows=fact_rows(flow=False, as_of=AS_OF, max_rows=12))
def test_filter_as_of_and_the_detector_keep_the_same_facts(rows):
    pit = filter_as_of(_facts(rows), AS_OF)["facts"]
    kept_pit = pit.get("us-gaap", {}).get("Assets", {}).get("units", {}).get("USD", [])
    kept_det = _eligible(rows)
    key = lambda r: (r["end"], r.get("filed"), r["val"], r["accn"])  # noqa: E731
    assert sorted(map(key, kept_pit)) == sorted(map(key, kept_det))
    # And both keep exactly the dated facts filed on or before the date.
    expected = [r for r in rows if r.get("filed") and r["filed"] <= AS_OF.isoformat()]
    assert sorted(map(key, kept_pit)) == sorted(map(key, expected))


class TestSameDayTieRule:
    """Two facts for one period filed the SAME day (a 10-Q and its /A, or a
    duplicate row) must resolve to the same value everywhere, by the shared
    `precedence` order: a same-day amendment supersedes its original (Hermes
    round 3 — the old first-at-the-date rule scored the 10-Q and hid the
    /A). The detector's `current` and the vintage store's `_take` must match
    the mapper, or the evidence names a value the score did not use."""

    def _rows(self):
        end = date(2025, 6, 30)
        return [
            _fact(end, 100.0, date(2025, 8, 1), accn="orig"),
            _fact(end, 111.0, date(2025, 8, 10), accn="first-same-day"),
            _fact(end, 222.0, date(2025, 8, 10), accn="second-same-day", form="10-Q/A"),
        ]

    def test_mapper_takes_the_same_day_amendment(self):
        facts = _collect(_facts(self._rows()), "us-gaap", "Assets", "USD")
        best = _dedupe_latest_filed(facts)
        assert best[(None, date(2025, 6, 30))].val == 222.0

    def test_the_amendment_wins_in_either_input_order(self):
        rows = self._rows()
        rows[1], rows[2] = rows[2], rows[1]
        best = _dedupe_latest_filed(_collect(_facts(rows), "us-gaap", "Assets", "USD"))
        assert best[(None, date(2025, 6, 30))].val == 222.0

    def test_detector_current_agrees(self):
        fps = rs.detect_restatements(_facts(self._rows()), selected_tags={"total_assets": "us-gaap:Assets"})
        assert len(fps) == 1
        assert fps[0].current_value == 222.0
        assert fps[0].current_accession == "second-same-day"
        assert fps[0].is_amendment

    def test_vintage_store_agrees(self):
        series = vs._series(_facts(self._rows()), scored_only=True)
        (k, v), = [(k, v) for k, v in series.items() if k[0] == "total_assets"]
        assert v["val"] == 222.0 and v["accn"] == "second-same-day"

    @given(rows=same_day_trails())
    def test_agreement_on_random_same_day_trails(self, rows):
        end = date(2025, 6, 30)
        mapper = _dedupe_latest_filed(_collect(_facts(rows), "us-gaap", "Assets", "USD"))[(None, end)]
        store = [v for k, v in vs._series(_facts(rows), scored_only=True).items() if k[0] == "total_assets"][0]
        assert store["val"] == mapper.val
        fps = rs.detect_restatements(_facts(rows), selected_tags={"total_assets": "us-gaap:Assets"})
        if fps:  # a footprint exists only when the values differ materially
            assert fps[0].current_value == mapper.val
