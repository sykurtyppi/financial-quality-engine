"""Every table that names a metric or a signal must name one the metrics
registry (`app/services/metrics_registry.py`) knows, and the registry's lists
must be what the engine actually emits.

Each assertion below corresponds to a drift that existed, silently, before
the registry: flag phrases for metrics no longer scored, a mismatch trigger
that could never fire, a Tier-1 signal named for the wrong 8-K item, and a
plan to make narrative metrics lockable that would have sealed commitments
the resolver cannot evaluate.
"""

from __future__ import annotations

import pytest

from app.core import pipeline
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import compute_metrics
from app.services.journal import vocabulary
from app.services.journal.resolver import _lookup_metric_value
from app.services.journal.schema_v2 import Assumption, BeforeBlock, can_lock
from app.services.metrics_registry import (
    FINANCIAL_METRICS,
    JOURNAL_LOCKABLE,
    NARRATIVE_METRICS,
    SIGNAL_KINDS,
    all_metric_names,
    scored_metric_names,
)
from app.services.narrative import mismatch
from app.services.narrative.narrative_metrics import _MISSING_SPECS
from app.services.reporting.decision_card import TIER1_SIGNALS, TIER3_SIGNALS
from app.services.scoring.thermometer import DISTRESS_CLUSTERS
from tests.fixtures.companies import stretch_dataset

# --- the registry matches what is emitted -------------------------------------


def test_financial_metrics_are_exactly_what_the_formula_registry_emits():
    emitted = set(compute_metrics(stretch_dataset()).history)
    assert emitted == set(FINANCIAL_METRICS), (
        f"registry only: {sorted(set(FINANCIAL_METRICS) - emitted)}; "
        f"emitted only: {sorted(emitted - set(FINANCIAL_METRICS))}"
    )


def test_narrative_metrics_are_exactly_what_the_narrative_layer_emits():
    assert set(NARRATIVE_METRICS) == set(_MISSING_SPECS)


def test_planes_do_not_overlap():
    assert not FINANCIAL_METRICS & NARRATIVE_METRICS
    assert not SIGNAL_KINDS & all_metric_names()


def test_scored_names_are_registered_and_read_from_the_config():
    scored = scored_metric_names()
    assert scored <= all_metric_names()
    assert scored & NARRATIVE_METRICS == {
        "kpi_removals", "disclosure_volume_change", "risk_factor_expansion",
    }
    assert scored == {
        "total_accruals", "accrual_trend", "beneish_m_score",
        "receivables_growth_spread", "dso_trend",
        "cfo_to_net_income", "fcf_margin", "fcf_margin_trend",
        "inventory_growth_spread", "dio_trend",
        "diluted_share_growth", "issuance_pressure",
        "capex_growth_spread", "capex_to_da", "capex_intensity_regime_shift",
        "net_debt_to_ebitda", "interest_coverage", "current_ratio",
        "debt_to_assets", "leverage_change",
        "kpi_removals", "disclosure_volume_change", "risk_factor_expansion",
    }


def test_scored_names_follow_a_config_change(monkeypatch):
    """Derived at call time, never cached: the scoring config stays the only
    authority on what is scored."""
    from app.config import scoring_config as cfg

    monkeypatch.setattr(cfg, "BLOCKS", cfg.BLOCKS[:1])
    assert scored_metric_names() == {"total_accruals", "accrual_trend", "beneish_m_score"}


def test_zero_weight_components_are_not_flaggable(monkeypatch):
    """A zero-weight component is scored for display but never flagged
    (P0-13), so the flag-phrase check must see only weighted names. No
    component has zero weight today; construct one."""
    from dataclasses import replace

    from app.config import scoring_config as cfg

    first = cfg.BLOCKS[0]
    zeroed = replace(first, metrics=[replace(first.metrics[0], weight=0.0), *first.metrics[1:]])
    monkeypatch.setattr(cfg, "BLOCKS", [zeroed, *cfg.BLOCKS[1:]])
    assert "total_accruals" in scored_metric_names()
    assert "total_accruals" not in scored_metric_names(positive_weight_only=True)


# --- consumers name only registered things -------------------------------------


def test_flag_phrases_name_only_weighted_scored_metrics():
    """Flags are drawn from scored components only (`_generate_flags`); a
    phrase for anything else is text no report can ever show."""
    assert set(pipeline._FLAG_PHRASES) <= scored_metric_names(positive_weight_only=True)


def test_change_list_reads_financial_metrics():
    """`_what_changed` reads the financial bundle's history."""
    assert {name for name, _, _ in pipeline._CHANGE_METRICS} <= FINANCIAL_METRICS


def test_distress_clusters_name_scored_metrics():
    members = {m for ms in DISTRESS_CLUSTERS.values() for m in ms}
    assert members <= scored_metric_names()


def test_mismatch_triggers_can_fire():
    """Concern-triggered specs read `concern_by_name`, which the pipeline fills
    only for scored metrics — an unscored trigger can never fire. The buyback
    spec triggers on the raw value instead, which only needs the metric to
    exist."""
    scored = scored_metric_names()
    for spec in mismatch.MISMATCH_SPECS:
        if spec.kind == "buyback_narrative_vs_share_count":
            assert set(spec.metric_names) <= FINANCIAL_METRICS
        else:
            assert set(spec.metric_names) <= scored, spec.kind


def test_the_concern_map_carries_scored_metrics_only():
    """Why an unscored trigger is dead: the concern map the mismatch layer
    reads has no entry for it however extreme its value."""
    metrics = {
        name: MetricResult(name=name, formula="f", fiscal_label="FY2025Q4",
                           status=MetricStatus.OK, value=-50.0)
        for name in FINANCIAL_METRICS
    }
    concerns = pipeline._financial_concerns(metrics)
    assert set(concerns) <= scored_metric_names()
    assert "fcf_to_net_income" not in concerns


def test_card_tiers_name_registered_metrics_or_signals():
    assert TIER1_SIGNALS | TIER3_SIGNALS <= all_metric_names() | SIGNAL_KINDS
    # Tier 1 is validated events and findings, never a score.
    assert not TIER1_SIGNALS & all_metric_names()
    # Item 4.01 is an auditor change; Item 4.02 is non-reliance.
    assert "auditor_change_8k_401" in TIER1_SIGNALS
    assert "auditor_change_8k_402" not in TIER1_SIGNALS


# --- the journal can lock only what it can resolve ------------------------------


def test_vocabulary_is_the_registrys_lockable_set():
    assert vocabulary.METRIC_IDS is JOURNAL_LOCKABLE
    assert JOURNAL_LOCKABLE == FINANCIAL_METRICS


def _before(metric: str) -> BeforeBlock:
    return BeforeBlock(
        thesis="a thesis long enough to satisfy the validator",
        conviction=3,
        intended_action="hold",
        assumptions=[Assumption(metric=metric, comparator=">", threshold=1.0,
                                window="FY2027Q3", resolve_by="2026-11-25")],
    )


@pytest.mark.parametrize("name", sorted(NARRATIVE_METRICS))
def test_narrative_metrics_are_refused_at_lock(name):
    """Scored or not, a narrative metric is computed from documents, and the
    resolver never sees one: locked, it would resolve `unresolvable`."""
    ok, reason = can_lock(_before(name))
    assert not ok and name in reason
    dataset = stretch_dataset()
    period = dataset.periods[-1]
    value, _note, structural = _lookup_metric_value(name, period, compute_metrics(dataset))
    assert value is None and structural


def test_a_financial_metric_still_locks():
    assert can_lock(_before("cfo_to_net_income"))[0]
