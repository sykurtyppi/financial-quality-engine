"""The one list of metric and signal names the engine knows.

Metric names were held in several tables in different modules — the
scoring config, the journal vocabulary, the narrative layer's missing-metric
specs, the pipeline's flag phrases and change list, the mismatch specs, the
distress clusters, the decision card's tiers — and none of them owned the
set. Names drifted: flag phrases for metrics no longer scored, a mismatch
trigger on a metric that can never carry a concern, a Tier-1 signal named
for the wrong 8-K item. Nothing failed, because nothing compared them.

This module is a table, not a computation: importing it runs no formula, so
a schema validator can read it. Every other table that names a metric or a
signal must name one listed here; `tests/unit/test_metrics_registry.py`
holds each of them to it, and pins the lists below to what the formula
registry and the narrative layer actually emit.

What is SCORED is not restated here — `scored_metric_names()` reads the
scoring config, which stays the only authority on it.
"""

from __future__ import annotations

from enum import StrEnum

# Every metric id `formulas.registry.compute_metrics` emits (bundle.history
# keys), scored or descriptive.
FINANCIAL_METRICS: frozenset[str] = frozenset({
    "accrual_trend", "asset_quality_proxy", "beneish_aqi", "beneish_depi",
    "beneish_dsri", "beneish_gmi", "beneish_lvgi", "beneish_m_score",
    "beneish_sgai", "beneish_sgi", "beneish_tata", "buyback_offset_ratio",
    "capex_growth_spread", "capex_intensity_regime_shift", "capex_to_da",
    "capex_to_revenue", "cfo_to_net_income", "current_ratio", "debt_to_assets",
    "deferred_revenue_growth_spread", "diluted_share_growth", "dio", "dio_trend",
    "dpo", "dso", "dso_trend", "fcf_margin", "fcf_margin_trend",
    "fcf_to_net_income", "goodwill_growth", "incremental_revenue_per_capex",
    "intangibles_to_assets", "interest_coverage", "inventory_growth_spread",
    "issuance_pressure", "leverage_change", "net_debt_to_ebitda",
    "net_share_count_change", "receivables_growth_spread", "sbc_to_cfo",
    "sbc_to_revenue", "total_accruals", "working_capital_swing_to_income",
})

# Every metric the narrative layer emits (`narrative_metrics._MISSING_SPECS`).
NARRATIVE_METRICS: frozenset[str] = frozenset({
    "adjustment_recurrence_ratio", "recurring_adjustment_terms", "kpi_removals",
    "disclosure_volume_change", "defensive_tone_change", "guidance_shift",
    "risk_factor_expansion",
})

# Evidence the decision card tiers by name that is not a metric: validated
# narrative findings, the shelved KPI-definition detector, and the event
# streams (restatement footprints, 8-K Item 4.02 non-reliance, Item 4.01
# auditor change, NT late-filing notices).
SIGNAL_KINDS: frozenset[str] = frozenset({
    "high_severity_disclosure",
    "kpi_definition_change",
    "restatement_footprint",
    "non_reliance_8k_402",
    "auditor_change_8k_401",
    "missed_deadline_nt",
})

# What a preregistered journal assumption may name as an engine metric.
# Narrative metrics are deliberately NOT lockable although three are scored:
# `resolver._lookup_metric_value` reads only the financial metrics bundle and
# the period's XBRL fields — it never sees a document — so a narrative name
# would seal cleanly and then resolve `unresolvable`, a case lost to a
# commitment that could never be evaluated.
JOURNAL_LOCKABLE: frozenset[str] = FINANCIAL_METRICS


class Basis(StrEnum):
    """Which periods a financial metric reads, as `formulas.registry` pairs
    them — what `provenance.sources_for` needs to name the filings behind a
    metric. `<field>` inputs are the metric's own period; `<field>_prior`
    inputs are the comparison period."""

    TTM = "ttm"  # flows summed over the 4 quarters ending here; instants at the end quarter
    ACCRUALS = "accruals"  # TTM, with the prior instant one year earlier (Sloan's average assets)
    TTM_YOY = "ttm_yoy"  # TTM here vs the TTM window ending 4 quarters earlier (Beneish)
    COMPOSITE = "composite"  # built from other metrics (the M-score from its indices)
    YOY = "yoy"  # this quarter vs the same quarter one year earlier
    PAIR = "pair"  # this quarter vs the previous quarter
    SERIES = "series"  # a statistic over the history of another metric


BASIS: dict[str, Basis] = {
    **{n: Basis.TTM for n in ("cfo_to_net_income", "fcf_to_net_income", "fcf_margin",
                              "net_debt_to_ebitda")},
    "total_accruals": Basis.ACCRUALS,
    **{n: Basis.TTM_YOY for n in ("beneish_dsri", "beneish_gmi", "beneish_aqi", "beneish_sgi",
                                  "beneish_depi", "beneish_sgai", "beneish_tata", "beneish_lvgi")},
    "beneish_m_score": Basis.COMPOSITE,
    **{n: Basis.YOY for n in ("receivables_growth_spread", "inventory_growth_spread",
                              "deferred_revenue_growth_spread", "capex_growth_spread")},
    **{n: Basis.PAIR for n in (
        "dso", "dio", "dpo", "working_capital_swing_to_income", "interest_coverage",
        "debt_to_assets", "current_ratio", "asset_quality_proxy", "intangibles_to_assets",
        "goodwill_growth", "leverage_change", "sbc_to_revenue", "sbc_to_cfo",
        "diluted_share_growth", "net_share_count_change", "buyback_offset_ratio",
        "issuance_pressure", "capex_to_revenue", "capex_to_da",
    )},
    **{n: Basis.SERIES for n in ("accrual_trend", "dso_trend", "dio_trend", "fcf_margin_trend",
                                 "capex_intensity_regime_shift", "incremental_revenue_per_capex")},
}


def all_metric_names() -> frozenset[str]:
    return FINANCIAL_METRICS | NARRATIVE_METRICS


def scored_metric_names(*, positive_weight_only: bool = False) -> frozenset[str]:
    """The metrics the scoring config places in a block, read from the config
    at call time (imported here, not at module load, so importing the
    registry pulls in nothing from scoring)."""
    from app.config import scoring_config as cfg

    return frozenset(
        ms.metric_name
        for block in cfg.BLOCKS
        for ms in block.metrics
        if not positive_weight_only or ms.weight > 0
    )
