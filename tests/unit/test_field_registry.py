"""The field registry (`app/services/ingestion/fields.py`) is the one source of
the engine's field ontology; the mapper's old literal tables are views of it.

1. Legacy equality — the views equal, IN ORDER, the literal tables they
   replaced (copied verbatim from main @ 904cf86 below). Candidate order breaks
   coverage ties and field order fixes diagnostic, note and warning order, so a
   silent reorder would move numbers or text without failing anything else.
   Delete the LEGACY_* copies once selection objects (plan PR 1.4) stop
   reading the views.
2. Conformance — the registry agrees with the schema, the TTM constructor, and
   its own structural rules.
3. The two payload trimmers (PIT and the fixture script) keep the same tags.
"""

from __future__ import annotations

import importlib.util
import types
import typing
from pathlib import Path

import pytest

from app.schemas.financials import PeriodFinancials
from app.services.backtesting.pit import mapped_tags
from app.services.formulas import ttm
from app.services.ingestion import companyfacts_mapper as m
from app.services.ingestion import fields as F
from app.services.ingestion import restatements

ROOT = Path(__file__).resolve().parents[2]

# --- verbatim from companyfacts_mapper.py @ 904cf86 (lines 57-180) ----------
LEGACY_INSTANT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "total_assets": (("us-gaap", "Assets"),),
    "current_assets": (("us-gaap", "AssetsCurrent"),),
    "cash_and_equivalents": (
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "receivables": (
        ("us-gaap", "AccountsReceivableNetCurrent"),
        ("us-gaap", "ReceivablesNetCurrent"),
        ("us-gaap", "AccountsAndOtherReceivablesNetCurrent"),
        ("us-gaap", "AccountsNotesAndLoansReceivableNetCurrent"),
    ),
    "inventory": (("us-gaap", "InventoryNet"), ("us-gaap", "InventoryGross")),
    "ppe_net": (
        ("us-gaap", "PropertyPlantAndEquipmentNet"),
        (
            "us-gaap",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        ),
    ),
    "intangible_assets": (
        ("us-gaap", "FiniteLivedIntangibleAssetsNet"),
        ("us-gaap", "IntangibleAssetsNetExcludingGoodwill"),
    ),
    "goodwill": (("us-gaap", "Goodwill"),),
    "current_liabilities": (("us-gaap", "LiabilitiesCurrent"),),
    "accounts_payable": (
        ("us-gaap", "AccountsPayableCurrent"),
        ("us-gaap", "AccountsPayableAndAccruedLiabilitiesCurrent"),
        ("us-gaap", "AccountsPayableTradeCurrent"),
    ),
    "deferred_revenue": (
        ("us-gaap", "ContractWithCustomerLiabilityCurrent"),
        ("us-gaap", "DeferredRevenueCurrent"),
    ),
    "shares_outstanding": (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ),
}

LEGACY_FLOW_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
    ),
    "cost_of_revenue": (
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfGoodsSold"),
        ("us-gaap", "CostOfSales"),
    ),
    "sga_expense": (("us-gaap", "SellingGeneralAndAdministrativeExpense"),),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "ebit": (("us-gaap", "OperatingIncomeLoss"),),
    # 2026-09-23 (deliberate, protocol entry #8): `Depreciation` is no longer
    # an aggregate D&A candidate — it is the partial fallback after the
    # depreciation + amortization composite.
    "depreciation_amortization": (
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
    ),
    "interest_expense": (
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestExpenseDebt"),
    ),
    "net_income": (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
    "stock_based_compensation": (
        ("us-gaap", "ShareBasedCompensation"),
        ("us-gaap", "AllocatedShareBasedCompensationExpense"),
    ),
    "cfo": (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    "capex": (
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsForCapitalImprovements"),
    ),
    "buybacks": (("us-gaap", "PaymentsForRepurchaseOfCommonStock"),),
    "share_issuance_proceeds": (
        ("us-gaap", "ProceedsFromIssuanceOfCommonStock"),
        ("us-gaap", "ProceedsFromIssuanceOrSaleOfEquity"),
    ),
    "shares_diluted": (
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
    ),
}

LEGACY_SGA_COMPONENTS = (
    ("us-gaap", "SellingAndMarketingExpense"),
    ("us-gaap", "GeneralAndAdministrativeExpense"),
)
LEGACY_DA_COMPONENTS = (
    ("us-gaap", "Depreciation"),
    ("us-gaap", "AmortizationOfIntangibleAssets"),
)

# LongTermDebt is a TOTAL (current + noncurrent): used only when the split is
# unavailable, never alongside it (double counting).
LEGACY_DEBT_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
LEGACY_DEBT_CURRENT = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")
LEGACY_DEBT_TOTAL = ("LongTermDebt",)
# 2026-09-23 (deliberate, protocol entry #9): `DebtCurrent` is aggregate
# current debt — moved out of the short-term role into its own, exclusive
# role, and treated as lease-inclusive.
LEGACY_DEBT_SHORT = ("ShortTermBorrowings", "CommercialPaper")
LEGACY_DEBT_CURRENT_AGGREGATE = ("DebtCurrent",)

# Finance (capital) lease liabilities are a financing obligation and belong in
# total debt (P0-10). Operating-lease liabilities are deliberately EXCLUDED —
# a different economic commitment that credit leverage conventions keep apart.
LEGACY_FINANCE_LEASE_NONCURRENT = ("FinanceLeaseLiabilityNoncurrent",)
LEGACY_FINANCE_LEASE_CURRENT = ("FinanceLeaseLiabilityCurrent",)
# Debt tags that already embed capital/finance-lease obligations: adding the
# separately reported finance-lease liability on top would double-count.
LEGACY_LEASE_INCLUSIVE_DEBT_TAGS = frozenset(
    {"LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent",
     "DebtCurrent"}
)

# Weighted-average share counts are not additive across quarters: no Q4
# derivation, direct facts only.
LEGACY_NON_ADDITIVE_FLOWS = {"shares_diluted"}

LEGACY_SPLIT_ADJUSTED_FIELDS = frozenset({"shares_diluted", "shares_outstanding"})
LEGACY_COVER_DATE_TOLERANCE_DAYS = 60
LEGACY_CRITICAL_FIELDS = ("revenue", "net_income", "cfo", "total_assets")


def _legacy_unit_for(field_name: str) -> str:
    return "shares" if field_name in ("shares_outstanding", "shares_diluted") else "USD"


def _legacy_mapped_tags() -> set[tuple[str, str]]:
    tags: set[tuple[str, str]] = set()
    for cands in list(LEGACY_INSTANT_FIELDS.values()) + list(LEGACY_FLOW_FIELDS.values()):
        tags.update(cands)
    tags.update(LEGACY_SGA_COMPONENTS)
    tags.update(LEGACY_DA_COMPONENTS)
    for tag in (
        LEGACY_DEBT_NONCURRENT + LEGACY_DEBT_CURRENT + LEGACY_DEBT_TOTAL + LEGACY_DEBT_SHORT
        + LEGACY_DEBT_CURRENT_AGGREGATE
        + LEGACY_FINANCE_LEASE_NONCURRENT + LEGACY_FINANCE_LEASE_CURRENT
    ):
        tags.add(("us-gaap", tag))
    return tags


# --- 1. legacy equality --------------------------------------------------------

def test_candidate_tables_are_unchanged_including_order():
    assert list(m.INSTANT_FIELDS.items()) == list(LEGACY_INSTANT_FIELDS.items())
    assert list(m.FLOW_FIELDS.items()) == list(LEGACY_FLOW_FIELDS.items())


@pytest.mark.parametrize(
    "name",
    [
        "SGA_COMPONENTS", "DA_COMPONENTS", "DEBT_NONCURRENT", "DEBT_CURRENT", "DEBT_TOTAL",
        "DEBT_SHORT", "DEBT_CURRENT_AGGREGATE", "FINANCE_LEASE_NONCURRENT", "FINANCE_LEASE_CURRENT",
        "LEASE_INCLUSIVE_DEBT_TAGS", "NON_ADDITIVE_FLOWS", "COVER_DATE_TOLERANCE_DAYS",
    ],
)
def test_mapper_views_are_unchanged(name):
    view, legacy = getattr(m, name), globals()[f"LEGACY_{name}"]
    assert type(view) is type(legacy)
    assert view == legacy


def test_split_adjusted_fields_unchanged():
    assert restatements.SPLIT_ADJUSTED_FIELDS == LEGACY_SPLIT_ADJUSTED_FIELDS
    assert type(restatements.SPLIT_ADJUSTED_FIELDS) is frozenset


def test_critical_fields_unchanged_including_order():
    assert m.CRITICAL_FIELDS == LEGACY_CRITICAL_FIELDS


def test_units_unchanged():
    for name in [f.name for f in F.FIELDS] + ["not_a_field"]:
        assert m._unit_for(name) == _legacy_unit_for(name), name


def test_pit_trims_to_exactly_the_legacy_tag_set():
    """`pit.py` is flag-only: its tag set must not move by a single concept."""
    assert mapped_tags() == _legacy_mapped_tags()
    assert type(mapped_tags()) is set


# --- 2. conformance --------------------------------------------------------------

def _numeric_fields() -> list[str]:
    hints = typing.get_type_hints(PeriodFinancials)
    return [
        n for n in PeriodFinancials.model_fields
        if isinstance(hints[n], types.UnionType) and set(hints[n].__args__) == {float, type(None)}
    ]


def test_registry_covers_every_numeric_period_field_once():
    names = [f.name for f in F.FIELDS]
    assert len(names) == len(set(names))
    assert set(names) == set(_numeric_fields())


def test_ttm_sums_exactly_the_additive_flows():
    """`formulas/ttm.py` is flag-only and keeps its own tuple; it must name
    every additive flow, in registry order, and nothing else."""
    additive = tuple(f.name for f in F.FIELDS if f.kind is F.Kind.FLOW and f.additive)
    assert ttm.FLOW_FIELDS == additive


def test_strategies_are_well_formed():
    for spec in F.FIELDS:
        assert spec.strategies, spec.name
        for s in spec.strategies:
            tags = s.all_tags()
            assert len(tags) == len(set(tags)), spec.name
            if s.composition is F.Composition.DEBT_BREAKDOWN:
                assert not s.tags and s.roles, spec.name
                assert sum(r.required for r in s.roles) == 1, spec.name
                assert s.criterion is F.Criterion.FIRST_PRESENT_PER_DATE, spec.name
            else:
                assert s.tags and not s.roles, spec.name
            if s.composition is F.Composition.SUM_ALL_REQUIRED:
                assert len(s.tags) >= 2, spec.name
        # A single-tag strategy comes first: it is the candidate list, and a
        # composite is only ever its alternative. A second single-tag
        # strategy is a PARTIAL fallback (depreciation for D&A): it comes
        # last, after a composite, and is drawn from that composite's own
        # components — a piece of the whole, never another whole.
        kinds = [s.composition for s in spec.strategies]
        assert kinds.count(F.Composition.SINGLE) <= 2, spec.name
        if F.Composition.SINGLE in kinds:
            assert kinds[0] is F.Composition.SINGLE, spec.name
        if kinds.count(F.Composition.SINGLE) == 2:
            fallback = spec.strategies[-1]
            assert fallback.composition is F.Composition.SINGLE, spec.name
            composite = spec.strategies[-2]
            assert composite.composition is F.Composition.SUM_ALL_REQUIRED, spec.name
            assert set(fallback.tags) < set(composite.tags), spec.name
            assert F.partial_fallback(spec.name) == fallback.tags
            # and never also an aggregate candidate
            assert not set(fallback.tags) & set(spec.strategies[0].tags), spec.name
        if spec.lease_inclusive_tags:
            role_concepts = {c for s in spec.strategies for r in s.roles for _, c in r.candidates}
            assert spec.lease_inclusive_tags <= role_concepts


def test_every_field_is_reachable_by_the_mapper():
    """A spec the mapper never builds would be silently dropped."""
    in_tables = set(m.INSTANT_FIELDS) | set(m.FLOW_FIELDS)
    composed = {f.name for f in F.FIELDS
                if all(s.composition is F.Composition.DEBT_BREAKDOWN for s in f.strategies)}
    assert in_tables | composed == {f.name for f in F.FIELDS}
    assert composed == {"total_debt"}


def test_share_fields_and_critical_fields():
    for spec in F.FIELDS:
        assert (spec.unit == "shares") == spec.split_adjusted, spec.name
    assert set(F.CRITICAL_FIELDS) <= {f.name for f in F.FIELDS}


def test_all_tags_covers_every_view():
    everything = F.all_tags()
    for table in (m.INSTANT_FIELDS, m.FLOW_FIELDS):
        for cands in table.values():
            assert set(cands) <= everything
    assert set(m.SGA_COMPONENTS) | set(m.DA_COMPONENTS) <= everything
    for group in (m.DEBT_NONCURRENT, m.DEBT_CURRENT, m.DEBT_TOTAL, m.DEBT_SHORT,
                  m.FINANCE_LEASE_NONCURRENT, m.FINANCE_LEASE_CURRENT):
        assert {("us-gaap", t) for t in group} <= everything


def test_shared_debt_roles_agree_across_strategies():
    """`role_tags` reads the first strategy declaring a role; a role repeated
    in the fallback strategy must carry the same candidates."""
    by_role: dict[str, tuple] = {}
    for s in F.field("total_debt").strategies:
        for r in s.roles:
            assert by_role.setdefault(r.role, r.candidates) == r.candidates, r.role


def test_debt_roles_match_the_mapper_composition():
    """`composition.compose_total_debt` composes exactly these roles on each
    path: the split (noncurrent + either aggregate current debt, or the
    current portion + short-term borrowings; + finance leases) and the
    LongTermDebt fallback (total + short + finance leases). A role missing from one
    strategy is invisible to the views, which read the first strategy that
    declares it, so pin the shape directly."""
    shapes = [
        tuple((r.role, r.required) for r in s.roles)
        for s in F.field("total_debt").strategies
    ]
    assert shapes == [
        (("noncurrent", True), ("current_aggregate", False), ("current", False),
         ("short", False), ("finance_lease_nc", False), ("finance_lease_c", False)),
        (("total", True), ("short", False),
         ("finance_lease_nc", False), ("finance_lease_c", False)),
    ]


def test_registry_lookups_refuse_unknowns():
    with pytest.raises(KeyError):
        F.field("ebitda")
    with pytest.raises(KeyError):
        F.composite_components("revenue")
    with pytest.raises(KeyError):
        F.role_tags("total_debt", "operating_lease")


# --- 3. the two trimmers agree ---------------------------------------------------

def test_fixture_script_keeps_the_same_tags_as_pit():
    """The fixture script once trimmed without the finance-lease tags, so the
    committed fixtures could not exercise that branch. Both trimmers now read
    the registry."""
    spec = importlib.util.spec_from_file_location(
        "make_real_fixtures", ROOT / "scripts" / "make_real_fixtures.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.wanted_tags() == mapped_tags()
    assert ("us-gaap", "FinanceLeaseLiabilityCurrent") in mod.wanted_tags()
