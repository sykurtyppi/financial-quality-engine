"""Properties of `build_dataset` that the score depends on.

- Row order: companyfacts lists facts in no promised order. The mapper's only
  order-sensitive rule is the documented same-day tie (the first fact at the
  latest filed date wins); with no such tie, shuffling rows must not move a
  single value. Checked on generated payloads and on the real fixtures.
- Point-in-time: after `filter_as_of(f, d)`, no mapped value can come from a
  fact filed after `d` — every value the dataset holds exists among the
  facts filed by then. (The metamorphic `build_dataset(as_of=)` equality is
  plan item 2.2; this is its precondition.)
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion.companyfacts_mapper import build_dataset
from tests.strategies import AS_OF, companyfacts

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"


def _dump(facts):
    try:
        dataset, _diag = build_dataset(facts, "T", n_quarters=8)
    except ValueError:  # fewer than two quarter ends: refused, consistently
        return None
    return dataset.model_dump()


def _without_same_day_ties(facts):
    """Drop rows so no (concept, start, end) has two facts at its latest
    filed date — the one case where order is the documented tie-break."""
    for concept in facts["facts"]["us-gaap"].values():
        rows = concept["units"]["USD"]
        latest: dict = {}
        for r in rows:
            k = (r.get("start"), r["end"])
            if r.get("filed") and r["filed"] >= latest.get(k, ""):
                latest[k] = r["filed"]
        seen: set = set()
        kept = []
        for r in rows:
            k = (r.get("start"), r["end"])
            if r.get("filed") == latest.get(k):
                if k in seen:
                    continue
                seen.add(k)
            kept.append(r)
        concept["units"]["USD"] = kept
    return facts


def _with_assets(facts):
    """The mapper needs Assets instants to fix quarter ends; make sure a
    generated payload has enough of them to build at all most of the time."""
    concepts = facts["facts"]["us-gaap"]
    rows = concepts.setdefault("Assets", {"units": {"USD": []}})["units"]["USD"]
    for i, end in enumerate(("2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31")):
        rows.append({"end": end, "val": 1000.0 + i, "form": "10-Q", "accn": f"base{i}",
                     "filed": "2025-02-01"})
    return facts


@given(facts=companyfacts().map(_with_assets).map(_without_same_day_ties), data=st.data())
def test_row_order_does_not_move_any_value(facts, data):
    shuffled = copy.deepcopy(facts)
    for concept in shuffled["facts"]["us-gaap"].values():
        rows = concept["units"]["USD"]
        concept["units"]["USD"] = data.draw(st.permutations(rows))
    assert _dump(shuffled) == _dump(facts)


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
@settings(max_examples=20)
@given(data=st.data())
def test_real_fixture_values_do_not_depend_on_row_order(ticker, data):
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    baseline = _dump(facts)
    shuffled = copy.deepcopy(facts)
    for taxonomy in shuffled["facts"].values():
        for concept in taxonomy.values():
            for unit, rows in concept["units"].items():
                concept["units"][unit] = data.draw(st.permutations(rows))
    assert _dump(shuffled) == baseline


@given(facts=companyfacts().map(_with_assets))
def test_a_pit_dataset_holds_only_values_filed_by_the_date(facts):
    pit = filter_as_of(facts, AS_OF)
    dataset = _dump(pit)
    if dataset is None:
        return
    visible = {
        r["val"]
        for concept in pit["facts"].get("us-gaap", {}).values()
        for r in concept["units"]["USD"]
    }
    # Flows can be derived (YTD differencing, FY minus three quarters) and
    # composites summed, so a derived value need not appear verbatim; every
    # DIRECT instant value must, and none may exist only in later filings.
    later_only = {
        r["val"]
        for concept in facts["facts"]["us-gaap"].values()
        for r in concept["units"]["USD"]
        if r.get("filed", "9999") > AS_OF.isoformat()
    } - visible
    for period in dataset["periods"]:
        value = period["total_assets"]
        assert value is None or value in visible
        assert value not in later_only
