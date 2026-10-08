"""The mapper's selection is an object; the legacy string is rendered from it.

`FieldDiagnostic.tag_used` spelled a selection four ways and the detector
parsed it back, assuming us-gaap for every unqualified piece and choosing how
to rebuild the figure by the field's name. `SeriesSelection` carries the
qualified components and the composer; these tests hold the two
representations together while both exist, and hold the detector to the
same answer whichever it is given.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion.companyfacts_mapper import FieldDiagnostic, build_dataset
from app.services.ingestion.restatements import (
    _resolve_tags,
    composer_of,
    scan_restatements,
)
from app.services.ingestion.selection import (
    Composer,
    SeriesSelection,
    composer_for,
    parse_components,
)
from tests.fixtures import selection_cases

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"


def _golden_inputs() -> list[tuple[str, dict]]:
    """Every payload the selection snapshot is built from."""
    from datetime import date

    out = []
    for ticker in ("AAPL", "KO", "CRM"):
        facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
        out.append((f"real/{ticker}", facts))
        for cut in (date(2024, 6, 30), date(2025, 3, 31)):
            out.append((f"pit/{ticker}@{cut}", filter_as_of(facts, cut)))
    out += [(f"synthetic/{name}", build()) for name, build in selection_cases.CASES.items()]
    return out


INPUTS = _golden_inputs()


@pytest.mark.parametrize(("case", "facts"), INPUTS, ids=[c for c, _ in INPUTS])
def test_every_selection_round_trips_through_its_legacy_string(case, facts):
    _ds, diag = build_dataset(facts, "X")
    series = diag.selected_series()
    for d in diag.fields:
        assert series[d.field_name] is d.selection
        if d.selection is None:
            assert d.tag_used is None, (case, d.field_name)
            continue
        assert d.tag_used == d.selection.tag_used
        assert SeriesSelection.from_tag_used(d.field_name, d.tag_used) == d.selection, (
            case, d.field_name,
        )
        assert d.selection.composer is composer_for(d.field_name)


@pytest.mark.parametrize(
    ("field_name", "components", "legacy"),
    [
        ("total_assets", ("us-gaap:Assets",), "us-gaap:Assets"),
        ("shares_outstanding", ("dei:EntityCommonStockSharesOutstanding",),
         "dei:EntityCommonStockSharesOutstanding"),
        ("depreciation_amortization", ("us-gaap:Depreciation",), "us-gaap:Depreciation"),
        ("sga_expense", ("us-gaap:SellingAndMarketingExpense", "us-gaap:GeneralAndAdministrativeExpense"),
         "SellingAndMarketingExpense+GeneralAndAdministrativeExpense"),
        ("total_debt", ("us-gaap:LongTermDebt",), "LongTermDebt"),
        ("total_debt", ("us-gaap:LongTermDebtNoncurrent", "us-gaap:DebtCurrent"),
         "LongTermDebtNoncurrent+DebtCurrent"),
    ],
)
def test_each_grammar_renders_as_the_mapper_always_recorded_it(field_name, components, legacy):
    selection = SeriesSelection.of(field_name, components)
    assert selection.tag_used == legacy
    assert SeriesSelection.from_tag_used(field_name, legacy) == selection


def test_the_composer_follows_the_registry():
    assert composer_for("total_assets") is Composer.SINGLE
    assert composer_for("depreciation_amortization") is Composer.STRATEGY
    assert composer_for("sga_expense") is Composer.STRATEGY
    assert composer_for("total_debt") is Composer.DEBT
    assert composer_of(SeriesSelection.of("revenue", ("us-gaap:Revenues",))) is None
    debt = composer_of(SeriesSelection.of("total_debt", ()))
    assert debt({"LongTermDebtNoncurrent": 800.0, "LongTermDebtCurrent": 100.0,
                 "DebtCurrent": 150.0}) == (950.0, ("LongTermDebtNoncurrent", "DebtCurrent"))


def test_a_diagnostic_cannot_carry_a_tag_used_its_selection_does_not_render():
    selection = SeriesSelection.of("revenue", ("us-gaap:Revenues",))
    FieldDiagnostic(field_name="revenue", tag_used="us-gaap:Revenues", periods_filled=1,
                    periods_total=1, selection=selection)
    with pytest.raises(ValidationError, match="is not the selection"):
        FieldDiagnostic(field_name="revenue", tag_used="us-gaap:SalesRevenueNet",
                        periods_filled=1, periods_total=1, selection=selection)


@pytest.mark.parametrize(
    ("legacy", "components"),
    [
        # The pre-H2 debt grammar recorded an absent role as `none`.
        ("LongTermDebtNoncurrent+none+none", ("us-gaap:LongTermDebtNoncurrent",)),
        ("none+LongTermDebtCurrent", ("us-gaap:LongTermDebtCurrent",)),
        # Blank pieces (a stray or doubled `+`, surrounding space) name nothing.
        ("A+ +B", ("us-gaap:A", "us-gaap:B")),
        ("A++B", ("us-gaap:A", "us-gaap:B")),
        (" us-gaap:Assets ", ("us-gaap:Assets",)),
        # A half-qualified piece names no concept.
        ("dei:+:X+A", ("us-gaap:A",)),
        ("none", ()),
        ("", ()),
        (None, ()),
    ],
)
def test_none_and_blank_pieces_are_not_components(legacy, components):
    """Hermes audit round 4: a `none` or blank piece surviving as a
    component survived the suite; the detector would then look for a
    concept called `us-gaap:none` beside the real ones."""
    assert parse_components(legacy) == components
    selection = SeriesSelection.from_tag_used("total_debt", legacy)
    assert (selection.components if selection else ()) == components


def test_an_object_keeps_its_taxonomy_where_a_bare_string_could_not():
    selection = SeriesSelection.of("shares_outstanding", ("dei:EntityCommonStockSharesOutstanding",))
    assert _resolve_tags({}, "shares_outstanding", (), "shares", None,
                         {"shares_outstanding": selection}) == [
        ("dei", "EntityCommonStockSharesOutstanding")
    ]
    # The bare legacy form can only assume us-gaap.
    assert _resolve_tags({}, "shares_outstanding", (), "shares", None,
                         {"shares_outstanding": "EntityCommonStockSharesOutstanding"}) == [
        ("us-gaap", "EntityCommonStockSharesOutstanding")
    ]


@pytest.mark.parametrize(("case", "facts"), INPUTS, ids=[c for c, _ in INPUTS])
def test_the_restatement_scan_is_the_same_given_objects_or_strings(case, facts):
    _ds, diag = build_dataset(facts, "X")
    by_object = scan_restatements(facts, selected_tags=diag.selected_series())
    by_string = scan_restatements(facts, selected_tags=diag.selected_tags())
    assert by_object == by_string


def _amended(facts: dict) -> dict:
    """The payload with every concept's value at one reported quarter end
    re-filed 10% higher by a later 10-Q/A — so every field, composed or not,
    carries a revision for the detector to find."""
    import copy

    out = copy.deepcopy(facts)
    target = selection_cases.QUARTER_ENDS[-2].isoformat()
    for tags in out["facts"].values():
        for concept in tags.values():
            for rows in concept["units"].values():
                rows += [
                    dict(r, val=r["val"] * 1.1, filed="2025-06-30", form="10-Q/A", accn="amend")
                    for r in list(rows) if r.get("end") == target and "val" in r
                ]
    return out


SYNTHETIC = [(name, _amended(build())) for name, build in selection_cases.CASES.items()]


@pytest.mark.parametrize(("case", "facts"), SYNTHETIC, ids=[c for c, _ in SYNTHETIC])
def test_amended_payloads_scan_the_same_given_objects_or_strings(case, facts):
    _ds, diag = build_dataset(facts, "X")
    by_object = scan_restatements(facts, selected_tags=diag.selected_series())
    assert by_object == scan_restatements(facts, selected_tags=diag.selected_tags())


def test_the_amended_cases_exercise_every_composer():
    fields = set()
    for _case, facts in SYNTHETIC:
        _ds, diag = build_dataset(facts, "X")
        fields |= {f.field_name for f in scan_restatements(
            facts, selected_tags=diag.selected_series()).footprints}
    assert {"total_debt", "sga_expense", "depreciation_amortization", "revenue"} <= fields
