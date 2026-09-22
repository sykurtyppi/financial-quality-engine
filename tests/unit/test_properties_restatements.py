"""Properties of the restatement scan that past defects broke.

- Point-in-time: a report dated `as_of` must see exactly what was filed by
  then. PR #36 fixed tag selection reading facts filed AFTER the date; the
  metamorphic form below catches any path that reads the payload before the
  PIT filter, not just the one that was fixed.
- Order: a composite's aggregate cannot depend on the order its components
  are named in (fd7aa0c; the `sorted(per_component)` guarantee was once lost
  with its test and found only by a mutation sweep).
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion.restatements import scan_restatements
from tests.strategies import AS_OF, SELECTED, companyfacts, fact_rows

SINCE = date(2022, 1, 1)


def _scan(facts, *, as_of, tags):
    return scan_restatements(facts, period_since=SINCE, as_of=as_of, selected_tags=tags).footprints


@given(facts=companyfacts(), tags=st.sampled_from([None, SELECTED]))
def test_scanning_as_of_a_date_equals_scanning_what_was_filed_by_then(facts, tags):
    dated = _scan(facts, as_of=AS_OF, tags=tags)
    filtered = filter_as_of(facts, AS_OF)
    assert _scan(filtered, as_of=None, tags=tags) == dated
    assert _scan(filtered, as_of=AS_OF, tags=tags) == dated


@given(facts=companyfacts(), late=fact_rows(flow=False, max_rows=4),
       tags=st.sampled_from([None, SELECTED]))
def test_a_fact_filed_after_the_date_changes_nothing(facts, late, tags):
    """Monotonicity in the future: however many later filings land — new
    values, new amendments, a newly used tag — a report dated `as_of` is
    unchanged."""
    before = _scan(facts, as_of=AS_OF, tags=tags)
    for row in late:
        row["filed"] = (AS_OF + timedelta(days=1 + len(row["accn"]))).isoformat()
    concepts = facts["facts"]["us-gaap"]
    for concept in ("Assets", "SalesRevenueNet", "LongTermDebtCurrent"):
        rows = concepts.setdefault(concept, {"units": {"USD": []}})["units"]["USD"]
        rows.extend(dict(r, accn=f"late-{concept}-{r['accn']}") for r in late)
    assert _scan(facts, as_of=AS_OF, tags=tags) == before


def _without_tag(footprints):
    return [{k: v for k, v in dataclasses.asdict(f).items() if k != "tag"} for f in footprints]


@given(facts=companyfacts(), as_of=st.sampled_from([AS_OF, None]))
def test_composite_footprints_do_not_depend_on_component_order(facts, as_of):
    """Exact float equality: the aggregate is summed in a fixed order however
    the selection names its components. Needs three components and values
    whose IEEE sum is order-sensitive — two terms commute exactly, so a
    two-component composite could never show the defect. Only the recorded
    tag string (which echoes the selection) may differ."""
    reordered = dict(SELECTED)
    reordered["sga_expense"] = "GeneralAndAdministrativeExpense+SellingAndMarketingExpense"
    reordered["total_debt"] = "CommercialPaper+LongTermDebtCurrent+LongTermDebtNoncurrent"
    assert _without_tag(_scan(facts, as_of=as_of, tags=SELECTED)) == _without_tag(
        _scan(facts, as_of=as_of, tags=reordered)
    )
