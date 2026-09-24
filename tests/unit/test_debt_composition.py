"""Total debt is composed by one rule, per balance-sheet date, in one place.

Hermes deep audit, finding 1: the mapper took, for each debt role, the first
tag with any value anywhere in the buffered window, so an abandoned tag hid
the one in use (Intel's commercial paper hid its aggregate current debt; KO's
pre-migration tags left every reported quarter empty), and aggregate current
debt (`DebtCurrent`) could be added on top of the current portion it already
contains. `composition.compose_total_debt` is now the rule, applied to what
was reported at each date, and the restatement detector applies the same
rule when it rebuilds the figure at each filing vintage.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.composition import (
    CURRENT,
    CURRENT_AGGREGATE,
    DEBT_TAGS,
    FINANCE_LEASE_CURRENT,
    FINANCE_LEASE_NONCURRENT,
    NONCURRENT,
    SHORT,
    SPLIT,
    SPLIT_AGGREGATE_CURRENT,
    TOTAL,
    TOTAL_FALLBACK,
    compose_total_debt,
)
from app.services.ingestion.restatements import (
    _composite_vintages,
    composer_of,
)
from app.services.ingestion.selection import SeriesSelection
from tests.fixtures import selection_cases

# --- the rule ---------------------------------------------------------------


def test_aggregate_current_debt_is_never_added_to_its_own_parts():
    # Hermes's controlled filing: 800 + 100 + 150 is 1,050; the total is 950.
    c = compose_total_debt({
        "LongTermDebtNoncurrent": 800.0, "LongTermDebtCurrent": 100.0, "DebtCurrent": 150.0,
        "CommercialPaper": 30.0,
    })
    assert c.total == 950.0 and c.strategy == SPLIT_AGGREGATE_CURRENT
    assert c.used == ("LongTermDebtNoncurrent", "DebtCurrent") and c.missing == ()


def test_split_sums_current_portion_and_short_term_borrowings():
    c = compose_total_debt({
        "LongTermDebtNoncurrent": 800.0, "LongTermDebtCurrent": 100.0, "CommercialPaper": 30.0,
    })
    assert c.total == 930.0 and c.strategy == SPLIT and c.missing == ()


def test_a_missing_role_counts_as_zero_and_is_named():
    c = compose_total_debt({"LongTermDebtNoncurrent": 800.0})
    assert c.total == 800.0 and c.missing == ("current", "short")


def test_the_first_candidate_reported_at_the_date_wins_within_a_role():
    c = compose_total_debt({
        "LongTermDebtNoncurrent": 1.0, "LongTermDebtAndCapitalLeaseObligations": 2.0,
        "ShortTermBorrowings": 10.0, "CommercialPaper": 20.0, "LongTermDebtCurrent": 0.5,
    })
    assert c.used == ("LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings")


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        # Lease-inclusive noncurrent: the noncurrent finance lease is inside it.
        ({"LongTermDebtAndCapitalLeaseObligations": 100.0, "LongTermDebtCurrent": 10.0,
          "FinanceLeaseLiabilityNoncurrent": 5.0, "FinanceLeaseLiabilityCurrent": 1.0}, 111.0),
        # DebtCurrent is "debt and lease obligation, classified as current".
        ({"LongTermDebtNoncurrent": 100.0, "DebtCurrent": 10.0,
          "FinanceLeaseLiabilityNoncurrent": 5.0, "FinanceLeaseLiabilityCurrent": 1.0}, 115.0),
        ({"LongTermDebtNoncurrent": 100.0, "LongTermDebtCurrent": 10.0,
          "FinanceLeaseLiabilityNoncurrent": 5.0, "FinanceLeaseLiabilityCurrent": 1.0}, 116.0),
    ],
)
def test_finance_leases_are_not_added_beside_a_tag_that_embeds_them(present, expected):
    assert compose_total_debt(present).total == expected


def test_long_term_debt_total_is_the_fallback_and_never_takes_debt_current():
    c = compose_total_debt({"LongTermDebt": 500.0, "DebtCurrent": 40.0, "CommercialPaper": 9.0})
    assert c.strategy == TOTAL_FALLBACK and c.total == 509.0
    assert "DebtCurrent" not in c.used
    # A noncurrent figure, when reported, always beats the total.
    assert compose_total_debt({"LongTermDebt": 500.0, "LongTermDebtNoncurrent": 450.0}).strategy == SPLIT


# Hermes audit round 4: a false `finance_lease_added` survived the suite —
# the flag drives the "finance leases added" note, so it is pinned on both
# strategies, with leases present, absent, and present but embedded.
@pytest.mark.parametrize(
    ("present", "added"),
    [
        ({"LongTermDebtNoncurrent": 100.0, "LongTermDebtCurrent": 10.0}, False),
        ({"LongTermDebtNoncurrent": 100.0, "LongTermDebtCurrent": 10.0,
          "FinanceLeaseLiabilityCurrent": 1.0}, True),
        ({"LongTermDebtNoncurrent": 100.0, "FinanceLeaseLiabilityNoncurrent": 5.0}, True),
        # Both leases present but both embedded (lease-inclusive noncurrent
        # and DebtCurrent): nothing is added.
        ({"LongTermDebtAndCapitalLeaseObligations": 100.0, "DebtCurrent": 10.0,
          "FinanceLeaseLiabilityNoncurrent": 5.0, "FinanceLeaseLiabilityCurrent": 1.0}, False),
        ({"LongTermDebt": 500.0}, False),
        ({"LongTermDebt": 500.0, "CommercialPaper": 9.0}, False),
        ({"LongTermDebt": 500.0, "FinanceLeaseLiabilityCurrent": 1.0}, True),
    ],
)
def test_finance_lease_added_says_whether_a_lease_was_added(present, added):
    c = compose_total_debt(present)
    assert c.finance_lease_added is added
    assert c.finance_lease_added == any(t.startswith("FinanceLease") for t in c.used)


def test_nothing_to_compose():
    assert compose_total_debt({}) is None
    assert compose_total_debt({"DebtCurrent": 5.0, "CommercialPaper": 1.0}) is None


def test_summation_order_does_not_depend_on_the_callers_mapping_order():
    tags = {"LongTermDebtNoncurrent": 0.1, "LongTermDebtCurrent": 0.2, "CommercialPaper": 0.3,
            "FinanceLeaseLiabilityNoncurrent": 0.7}
    forward = compose_total_debt(tags)
    backward = compose_total_debt(dict(reversed(list(tags.items()))))
    assert forward.total == backward.total and forward.used == backward.used


@given(
    st.dictionaries(
        st.sampled_from(DEBT_TAGS),
        st.floats(min_value=0.0, max_value=1e12, allow_nan=False),
    )
)
def test_no_double_counting_combination_is_ever_composed(present):
    c = compose_total_debt(present)
    if c is None:
        assert not any(t in present for t in NONCURRENT + TOTAL)
        return
    used = set(c.used)
    # Aggregate current debt stands alone on the current side.
    if used & set(CURRENT_AGGREGATE):
        assert not used & set(CURRENT + SHORT)
    # A total and a noncurrent figure are never summed.
    assert not (used & set(TOTAL) and used & set(NONCURRENT))
    # At most one candidate per role.
    for role in (NONCURRENT, TOTAL, CURRENT_AGGREGATE, CURRENT, SHORT,
                 FINANCE_LEASE_NONCURRENT, FINANCE_LEASE_CURRENT):
        assert len(used & set(role)) <= 1, role
    # Plain left-to-right addition in role order, as the mapper always did —
    # not `sum()`, which compensates float error since Python 3.12 and so
    # differs in the last bit.
    expected = 0.0
    for t in c.used:
        expected += present[t]
    assert c.total == expected


# --- mapper and detector agree ------------------------------------------------

DEBT_CASES = [name for name in selection_cases.CASES if name.startswith("debt_")]


@pytest.mark.parametrize("case", DEBT_CASES)
def test_the_detector_rebuilds_exactly_the_value_the_mapper_scored(case):
    facts = selection_cases.CASES[case]()
    ds, diag = build_dataset(facts, "X")
    selection = diag.field_by_name("total_debt").selection
    if selection is None:
        assert all(p.total_debt is None for p in ds.periods)
        return
    # (A single component is compared tag by tag by the detector; the
    # rebuild below reduces to that tag's latest value, so it is checked too.)
    rebuilt = _composite_vintages(facts, selection.concepts, "USD", None, compose=composer_of(selection))
    for p in ds.periods:
        vintages = rebuilt.get((None, p.period_end))
        if p.total_debt is None:
            assert not vintages
            continue
        latest = max(vintages, key=lambda v: v[0])
        assert latest[1] == p.total_debt, (case, p.period_end)


def test_summing_instead_of_composing_would_double_count():
    # The detector's plain sum is what it used to do for total_debt.
    facts = selection_cases.debt_hermes_double_count()
    series = [("us-gaap", t) for t in ("LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent")]
    q = selection_cases.QUARTER_ENDS[-1]
    summed = _composite_vintages(facts, series, "USD", None)[(None, q)][-1][1]
    debt = composer_of(SeriesSelection.of("total_debt", ()))
    composed = _composite_vintages(facts, series, "USD", None, compose=debt)[(None, q)][-1][1]
    assert (summed, composed) == (1_050.0, 950.0)


def test_the_restatement_scan_composes_debt_it_does_not_sum_it():
    # The current portion is reported at every quarter end and aggregate
    # current debt only at the latest ones, so the selection names both. At
    # the last quarter end a 10-Q/A amends DebtCurrent 150 -> 180: the scored
    # total moved 950 -> 980. Summing every named component that was filed
    # would report 1,050 -> 1,080 — a figure the engine never scored.
    from datetime import date

    from app.services.ingestion.restatements import detect_restatements

    q = selection_cases.QUARTER_ENDS
    p = selection_cases._base("Debt Scan Co")
    p.add("LongTermDebtNoncurrent", selection_cases._instants(800.0, step=0.0))
    p.add("LongTermDebtCurrent", selection_cases._instants(100.0, step=0.0))
    p.add("DebtCurrent", selection_cases._instants(150.0, q[9:], step=0.0)
          + [selection_cases.instant(q[-1], 180.0, filed=date(2025, 5, 1), form="10-Q/A")])
    facts = p.data
    _ds, diag = build_dataset(facts, "X")
    assert diag.field_by_name("total_debt").tag_used == (
        "LongTermDebtNoncurrent+DebtCurrent+LongTermDebtCurrent"
    )
    [fp] = [f for f in detect_restatements(facts, selected_tags=diag.selected_tags())
            if f.field_name == "total_debt"]
    assert (fp.period_end, fp.original_value, fp.current_value) == (q[-1], 950.0, 980.0)


def test_an_amendment_after_a_component_first_appears_is_still_found():
    # 2024-03-31: the noncurrent figure is filed first; the current portion
    # is first reported by a later filing, and then amended. The first
    # vintage is composed differently (noncurrent alone), so it is not
    # compared — but the amendment inside the stable composition is a
    # revision of the scored total: 900 -> 950.
    from datetime import date

    from app.services.ingestion.restatements import detect_restatements

    q = selection_cases.QUARTER_ENDS[8]
    p = selection_cases._base("Debt Adoption Co")
    p.add("LongTermDebtNoncurrent", selection_cases._instants(800.0, step=0.0))
    p.add("LongTermDebtCurrent", [
        *(selection_cases.instant(e, 100.0) for e in selection_cases.QUARTER_ENDS if e != q),
        selection_cases.instant(q, 100.0, filed=date(2024, 8, 1)),
        selection_cases.instant(q, 150.0, filed=date(2024, 11, 1), form="10-Q/A"),
    ])
    facts = p.data
    _ds, diag = build_dataset(facts, "X")
    [fp] = [f for f in detect_restatements(facts, selected_tags=diag.selected_tags())
            if f.field_name == "total_debt" and f.period_end == q]
    assert (fp.original_value, fp.current_value) == (900.0, 950.0)


def test_the_fallback_names_a_missing_short_term_role_only_when_it_is_missing():
    # Mutation backlog (Hermes audit round 5): `if short is None` negated on
    # the LongTermDebt fallback reported the reverse, and `missing` drives
    # the "short-term borrowings not reported" note.
    assert compose_total_debt({"LongTermDebt": 500.0}).missing == ("short",)
    assert compose_total_debt({"LongTermDebt": 500.0, "CommercialPaper": 9.0}).missing == ()
