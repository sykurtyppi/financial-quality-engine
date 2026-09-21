"""`can_lock`'s metric vocabulary must track what the resolver can resolve.

`vocabulary.METRIC_IDS` is written out by hand so a schema validator need not
run the formula registry. That is only safe while the two agree: a metric
added to the registry and not to the list would be refused at lock though the
resolver could resolve it, and a metric renamed in the registry would leave a
dead name that locks and then never resolves. This test is what keeps them
honest.
"""

from __future__ import annotations

import pytest

from app.schemas.financials import PeriodFinancials
from app.services.formulas.registry import compute_metrics
from app.services.journal.schema_v2 import Assumption, BeforeBlock, can_lock
from app.services.journal.vocabulary import (
    FIELD_NAMES,
    METRIC_IDS,
    is_resolvable_metric,
    is_wellformed_window,
)
from tests.fixtures.companies import stretch_dataset


def _before(**over) -> BeforeBlock:
    row = dict(metric="revenue", comparator=">", threshold=1.0,
               window="FY2027Q3", resolve_by="2026-11-25")
    row.update(over)
    return BeforeBlock(
        thesis="a thesis long enough to satisfy the validator",
        conviction=3,
        intended_action="hold",
        assumptions=[Assumption(**row)],
    )


def test_metric_ids_match_what_the_registry_emits():
    emitted = set(compute_metrics(stretch_dataset()).history)
    assert emitted == set(METRIC_IDS), (
        "vocabulary.METRIC_IDS has drifted from formulas.registry\n"
        f"  registry only: {sorted(emitted - set(METRIC_IDS))}\n"
        f"  vocabulary only: {sorted(set(METRIC_IDS) - emitted)}"
    )


def test_field_names_track_the_period_schema():
    declared = set(PeriodFinancials.model_fields)
    computed = {n for n in dir(PeriodFinancials)
                if isinstance(getattr(PeriodFinancials, n, None), property)}
    assert FIELD_NAMES <= (declared | computed)
    # The three descriptors are excluded on purpose: they identify a period
    # rather than measure one, and cannot be compared to a numeric threshold.
    assert {"fiscal_label", "period_end", "period_type"}.isdisjoint(FIELD_NAMES)


@pytest.mark.parametrize("name", [
    "revenue", "cfo", "dso", "leverage_change",
    # Computed properties: reachable by the resolver's `hasattr` lookup, so
    # they must be lockable too. Deriving the vocabulary from `model_fields`
    # alone refused all three.
    "ebitda", "fcf", "gross_profit",
])
def test_resolvable_names_lock(name):
    ok, why = can_lock(_before(metric=name))
    assert ok, why


@pytest.mark.parametrize("name", ["revneu_typo", "ebbitda", "free_cash_flow", "netincome"])
def test_unresolvable_names_are_refused_at_lock(name):
    assert not is_resolvable_metric(name)
    ok, why = can_lock(_before(metric=name or "x"))
    assert not ok and "not a resolvable name" in why


@pytest.mark.parametrize("window", ["FY2027Q3", "fy2027q3", "P2026-10-26"])
def test_wellformed_windows_lock(window):
    assert is_wellformed_window(window)
    ok, why = can_lock(_before(window=window))
    assert ok, why


@pytest.mark.parametrize("window", ["FQ3-27", "Q3 2027", "FY2027", "FY2027Q5", "2026-10-26"])
def test_malformed_windows_are_refused_at_lock(window):
    assert not is_wellformed_window(window)
    ok, why = can_lock(_before(window=window))
    assert not ok and "not a fiscal label" in why


def test_a_refused_window_would_otherwise_have_resolved_pending_forever():
    # The failure this guards: `_find_period` matches `fiscal_label` exactly,
    # so a branding-style window silently matches nothing rather than erroring.
    from app.services.journal.resolver import _find_period
    ds = stretch_dataset()
    assert _find_period(ds, "FQ3-27") is None
    assert _find_period(ds, ds.periods[-1].fiscal_label) is not None


def test_tightening_can_lock_does_not_invalidate_an_already_sealed_entry():
    """A validator that grew stricter must not retroactively break entries
    sealed under the looser rule — the seal is a hash over the BEFORE block,
    and `can_lock` is consulted only at the moment of locking. Anything else
    would destroy exactly the blind cases the journal exists to accumulate."""
    from datetime import date as d
    from datetime import datetime, timezone

    from app.services.journal.schema_v2 import EntryV2, hash_before, is_locked, verify_lock

    stale = BeforeBlock(
        thesis="a thesis long enough to satisfy the validator",
        conviction=3,
        intended_action="hold",
        # Shape `can_lock` now refuses; entries carrying it predate the rule.
        assumptions=[Assumption(metric="revenue", comparator=">", threshold=1.0,
                                window="Q", resolve_by=d(2026, 12, 31))],
    )
    assert not can_lock(stale)[0]

    sealed = EntryV2(
        ticker="NVDA", day=d(2026, 8, 1),
        opened=datetime(2026, 8, 1, tzinfo=timezone.utc),
        before=stale,
        before_sha256=hash_before(stale),
        locked_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert is_locked(sealed) and verify_lock(sealed)


def test_every_admitted_name_actually_resolves():
    """The vocabulary's whole job is to agree with the resolver. Derive the
    claim from the resolver itself rather than trusting a hand-kept list:
    a name `can_lock` admits must not come back `unresolvable`."""
    from app.services.journal.resolver import _lookup_metric_value
    from app.services.journal.vocabulary import RESOLVABLE_METRICS
    from app.services.formulas.registry import compute_metrics

    ds = stretch_dataset()
    period = ds.periods[-1]
    bundle = compute_metrics(ds)
    unresolvable = []
    for name in sorted(RESOLVABLE_METRICS):
        _value, _note, structural = _lookup_metric_value(name, period, bundle)
        if structural:
            unresolvable.append(name)
    assert not unresolvable, (
        f"can_lock admits names the resolver calls unresolvable: {unresolvable}"
    )


def test_a_resolver_reachable_property_is_not_refused():
    """Regression for the narrowing this file previously asserted as correct:
    `ebitda` was rejected at lock while the resolver resolved it fine."""
    from app.services.journal.resolver import _lookup_metric_value
    period = stretch_dataset().periods[-1]
    for name in ("ebitda", "fcf", "gross_profit"):
        value, _note, structural = _lookup_metric_value(name, period, None)
        assert not structural and value is not None
        assert can_lock(_before(metric=name))[0]


def test_ttm_metrics_are_exactly_the_unreachable_ones():
    """Derive the exclusion rather than trusting the written list: a metric is
    excluded iff no result it produces carries a label any dataset period has.
    When the resolver learns to address a TTM basis, this fails and the list
    shrinks — it cannot silently keep refusing names that became reachable."""
    from app.services.formulas.registry import compute_metrics
    from app.services.journal.vocabulary import TTM_BASIS_METRICS

    ds = stretch_dataset()
    period_labels = {p.fiscal_label for p in ds.periods}
    unreachable = {
        name for name, history in compute_metrics(ds).history.items()
        if not ({m.fiscal_label for m in history} & period_labels)
    }
    assert unreachable == set(TTM_BASIS_METRICS), (
        f"registry only: {sorted(unreachable - set(TTM_BASIS_METRICS))}\n"
        f"vocabulary only: {sorted(set(TTM_BASIS_METRICS) - unreachable)}"
    )


def test_a_ttm_metric_is_refused_rather_than_sealed_unresolvable():
    ok, why = can_lock(_before(metric="total_accruals"))
    assert not ok and "not a resolvable name" in why
