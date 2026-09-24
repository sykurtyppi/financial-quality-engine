"""SG&A and D&A are resolved per quarter, by one rule, in one place.

Hermes re-audit: D&A was chosen once for the whole window, so a filer that
reported amortization for only part of it (Alphabet) got depreciation alone
in every quarter — 7,104 where 7,471 was reported. `composition.
resolve_by_strategy` now resolves each quarter from the field's registry
strategies (own concept, then every component, then a partial fallback),
and the restatement detector rebuilds the figure with the same function.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.composition import (
    COMPOSITE,
    PARTIAL,
    SINGLE,
    resolve_by_strategy,
)
from app.services.ingestion.restatements import (
    _COMPOSERS,
    _composite_vintages,
    _parse_selection,
    detect_restatements,
)
from tests.fixtures import selection_cases

DA = "depreciation_amortization"

# --- the rule ---------------------------------------------------------------


def test_the_aggregate_wins_and_nothing_is_added_to_it():
    r = resolve_by_strategy(DA, {
        "DepreciationDepletionAndAmortization": 70.0, "Depreciation": 55.0,
        "AmortizationOfIntangibleAssets": 12.0,
    })
    assert (r.total, r.strategy, r.used) == (70.0, SINGLE, ("DepreciationDepletionAndAmortization",))


def test_both_components_compose():
    r = resolve_by_strategy(DA, {"Depreciation": 7_104.0, "AmortizationOfIntangibleAssets": 367.0})
    assert (r.total, r.strategy) == (7_471.0, COMPOSITE) and not r.partial


def test_depreciation_alone_is_partial():
    r = resolve_by_strategy(DA, {"Depreciation": 20.0})
    assert (r.total, r.strategy, r.partial) == (20.0, PARTIAL, True)


def test_amortization_alone_is_nothing():
    assert resolve_by_strategy(DA, {"AmortizationOfIntangibleAssets": 5.0}) is None
    assert resolve_by_strategy(DA, {}) is None


def test_sga_single_then_composite_and_never_partial():
    assert resolve_by_strategy("sga_expense", {
        "SellingGeneralAndAdministrativeExpense": 330.0, "SellingAndMarketingExpense": 200.0,
        "GeneralAndAdministrativeExpense": 100.0,
    }).total == 330.0
    assert resolve_by_strategy("sga_expense", {
        "SellingAndMarketingExpense": 200.0, "GeneralAndAdministrativeExpense": 100.0,
    }).strategy == COMPOSITE
    # SG&A has no partial fallback: half of it is not SG&A.
    assert resolve_by_strategy("sga_expense", {"SellingAndMarketingExpense": 200.0}) is None


_DA_TAGS = ["DepreciationDepletionAndAmortization", "Depreciation", "AmortizationOfIntangibleAssets"]


@given(st.dictionaries(st.sampled_from(_DA_TAGS), st.floats(min_value=0, max_value=1e12)))
def test_never_an_aggregate_with_its_components_and_partial_only_without_a_whole(present):
    r = resolve_by_strategy(DA, present)
    if r is None:
        assert "DepreciationDepletionAndAmortization" not in present and "Depreciation" not in present
        return
    if "DepreciationDepletionAndAmortization" in r.used:
        assert r.used == ("DepreciationDepletionAndAmortization",)
    if r.partial:
        assert "DepreciationDepletionAndAmortization" not in present
        assert "AmortizationOfIntangibleAssets" not in present


# --- mapper and detector agree ------------------------------------------------

CASES = ["tag_choice", "composites_lose", "da_split_equal_coverage", "da_aggregate_with_amortization",
         "da_amortization_partial", "da_google_pattern"]


@pytest.mark.parametrize("field_name", ["sga_expense", DA])
@pytest.mark.parametrize("case", CASES)
def test_the_detector_rebuilds_exactly_the_value_the_mapper_scored(case, field_name):
    facts = selection_cases.CASES[case]()
    ds, diag = build_dataset(facts, "X")
    selected = diag.field_by_name(field_name).tag_used
    if selected is None:
        return
    rebuilt = _composite_vintages(
        facts, _parse_selection(selected), "USD", None, compose=_COMPOSERS[field_name]
    )
    for p in ds.periods:
        mapped = getattr(p, field_name)
        start = date(p.period_end.year, p.period_end.month - 2, 1)
        vintages = rebuilt.get((start, p.period_end))
        if mapped is None:
            assert not vintages
            continue
        latest = max(vintages, key=lambda v: v[0])
        assert latest[1] == mapped, (case, field_name, p.period_end)


def test_the_restatement_scan_never_sums_an_aggregate_with_its_components():
    # All three D&A concepts filed at every quarter end; depreciation is then
    # amended. The scored figure is the aggregate, which did not move: no
    # footprint. Summing would have reported a revision of a figure never
    # scored.
    facts = selection_cases.da_aggregate_with_amortization()
    q = selection_cases.QUARTER_ENDS[-2]
    facts["facts"]["us-gaap"]["Depreciation"]["units"]["USD"].append(
        selection_cases.quarter(q, 999.0, filed=date(2025, 5, 1), form="10-Q/A")
    )
    _ds, diag = build_dataset(facts, "X")
    assert not [f for f in detect_restatements(facts, selected_tags=diag.selected_tags())
                if f.field_name == DA]
