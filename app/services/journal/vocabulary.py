"""The closed vocabulary a preregistered assumption may name.

`resolver._lookup_metric_value` resolves an assumption's `metric` against two
namespaces, in order: the engine metric ids the formula registry emits, then
the raw XBRL-mapped fields on `PeriodFinancials`. A name in neither is a spec
problem the resolver reports as `unresolvable` — but only when someone runs
`resolve`, which on the journal's protocol is weeks after the lock.

That is too late. A locked entry is hash-sealed and deliberately immutable, so
a typo caught at resolve time cannot be corrected without breaking the seal:
the case is simply lost. This module gives `can_lock` the vocabulary it needs
to refuse the typo at the only moment it is still fixable.

`METRIC_IDS` is written out rather than computed because computing it means
running the registry over a dataset, which a schema validator must not do.
`tests/unit/test_assumption_vocabulary.py` pins it to what the registry
actually emits, so a metric added or renamed in the registry fails there
instead of silently narrowing what an operator is allowed to commit to.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.schemas.financials import PeriodFinancials

# Engine metric ids from `formulas.registry.compute_metrics` (bundle.history keys).
METRIC_IDS: frozenset[str] = frozenset({
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

# Raw XBRL-mapped fields, minus the three that describe a period rather than
# measure one — `_lookup_metric_value` would happily return a date or a label
# and then fail to compare it against a numeric threshold.
_NON_MEASURES = {"fiscal_label", "period_end", "period_type"}


def _period_measures() -> frozenset[str]:
    """Everything on `PeriodFinancials` the resolver can read as a number.

    The resolver reaches fields with `hasattr`, which finds COMPUTED
    PROPERTIES (`ebitda`, `fcf`, `gross_profit`) as readily as declared
    fields. `model_fields` lists only the declared ones, so deriving the
    vocabulary from it alone refused three names the resolver resolves
    perfectly well — a validator stricter than the thing it guards, which
    blocks legitimate commitments instead of impossible ones.
    """
    declared = set(PeriodFinancials.model_fields)
    # Anything pydantic's own BaseModel defines (`model_extra`,
    # `model_fields_set`, ...) is plumbing, not a measurement.
    inherited = set(dir(BaseModel))
    computed = {
        name for name in dir(PeriodFinancials)
        if not name.startswith("_")
        and name not in inherited
        and isinstance(getattr(PeriodFinancials, name, None), property)
    }
    return frozenset(declared | computed) - _NON_MEASURES


FIELD_NAMES: frozenset[str] = _period_measures()

# Registry metrics computed on a trailing-twelve-month basis. Their results
# are labelled `TTM FY2025Q4`, while `resolver._find_period` matches a window
# against the dataset's QUARTERLY `fiscal_label` (`FY2025Q4`). Neither spelling
# reaches them: `FY2025Q4` finds the period but no TTM result carries that
# label, and `TTM FY2025Q4` matches no period at all.
#
# So these lock cleanly and then never resolve — the same failure as a
# branding-style window, and the reason this module exists. They are refused
# at lock until the resolver can address a TTM basis; until then, refusing is
# the honest answer rather than sealing a commitment that cannot terminate.
# `test_ttm_metrics_are_exactly_the_unreachable_ones` derives this set from
# the registry and the resolver, so it shrinks by itself once that is fixed.
TTM_BASIS_METRICS: frozenset[str] = frozenset({
    "accrual_trend", "beneish_aqi", "beneish_depi", "beneish_dsri",
    "beneish_gmi", "beneish_lvgi", "beneish_m_score", "beneish_sgai",
    "beneish_sgi", "beneish_tata", "cfo_to_net_income", "fcf_margin",
    "fcf_margin_trend", "fcf_to_net_income", "net_debt_to_ebitda",
    "total_accruals",
})

RESOLVABLE_METRICS: frozenset[str] = (METRIC_IDS - TTM_BASIS_METRICS) | FIELD_NAMES

# `companyfacts_mapper._fiscal_label` emits exactly two shapes: the structural
# fiscal label when the filer's fiscal year-end month is known, and a
# period-end fallback when it is not.
_WINDOW_RE = re.compile(r"^(FY\d{4}Q[1-4]|P\d{4}-\d{2}-\d{2})$")


def is_resolvable_metric(name: str) -> bool:
    return name.strip() in RESOLVABLE_METRICS


def is_wellformed_window(window: str) -> bool:
    """`resolver._find_period` matches `window` against `fiscal_label` by exact
    (case-insensitive) string equality. A window in any other shape does not
    raise — it simply matches no period and resolves `pending` forever, which
    is indistinguishable from `the filing has not landed yet`."""
    return bool(_WINDOW_RE.match(window.strip().upper()))
