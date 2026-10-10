"""The canonical field registry: which XBRL tags back each PeriodFinancials
field, how a field is composed when no single tag carries it, and the
per-field facts the rest of the engine keys on (unit, additivity, split
adjustment, cover-date tolerance).

Data only. Before this module the same ontology lived in ten literal tables
in `companyfacts_mapper`, one in `restatements`, and two hand-written tag
enumerations (`pit.mapped_tags`, `make_real_fixtures.wanted_tags`) that
re-walked those tables and drifted from them: the fixture script never
learned the finance-lease tags, and PIT once forgot them too. Every one of
those tables is now a view derived from `FIELDS`, under its old name and in
its old order, so importers are unchanged and the registry is the one place a
tag is added.

Order is load-bearing. Candidate order breaks coverage ties in the mapper;
`FIELDS` order is the mapper's build order, which fixes the order of
diagnostics, field notes and the selections the restatement detector reads.
Strategy order is preference order: the single tag first, the composite as
the alternative (today the mapper hard-codes the same rule — the composite
wins only on strictly greater coverage — and the planned selector will read
it from here). Do not sort anything here.

This module imports nothing from the ingestion package, so any of it may
import this without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

Tag = tuple[str, str]  # (taxonomy, concept), e.g. ("us-gaap", "Assets")

# The currency every monetary field is read in: the companyfacts unit the
# mapper collects (a fact reported only in another currency is not read).
# The valuation plane bridges a price to these figures only when the price
# is in it too: there is no FX conversion anywhere in the engine (Hermes
# audit of PR #118, finding 2: a price in EUR was multiplied by the share
# count and added to USD debt).
FILING_CURRENCY = "USD"


class Kind(str, Enum):
    INSTANT = "instant"
    FLOW = "flow"


class Composition(str, Enum):
    # One tag. Never mixed where the selected tag has a value; a quarter it
    # does not report is filled only from another candidate proven equal to
    # it on every shared quarter (one of them a non-zero reported quarter), and the diagnostics say so (the mapper's
    # `_fill_gaps`: a filer's tag switch).
    SINGLE = "single"
    SUM_ALL_REQUIRED = "sum_all_required"  # sum only where every component exists
    DEBT_BREAKDOWN = "debt_breakdown"  # required role + optional roles, missing -> 0


class Criterion(str, Enum):
    # most reported-window quarters covered wins; ties by extended-window
    # coverage, then candidate order
    BEST_COVERAGE = "best_coverage"
    # at each balance-sheet date, the first candidate reported AT THAT DATE
    # (composition.compose_total_debt). Not "first with any value anywhere":
    # a tag used only in old quarters hid the one in use today.
    FIRST_PRESENT_PER_DATE = "first_present_per_date"


@dataclass(frozen=True)
class RoleSpec:
    role: str
    candidates: tuple[Tag, ...]
    required: bool = False


@dataclass(frozen=True)
class SeriesStrategy:
    composition: Composition
    criterion: Criterion
    tags: tuple[Tag, ...] = ()
    roles: tuple[RoleSpec, ...] = ()

    def all_tags(self) -> tuple[Tag, ...]:
        return self.tags + tuple(t for r in self.roles for t in r.candidates)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: Kind
    strategies: tuple[SeriesStrategy, ...]
    unit: str = FILING_CURRENCY
    split_adjusted: bool = False  # a stock split rewrites history: not a revision
    additive: bool = True  # False: no Q4 derivation, no TTM sum
    cover_date_tolerance_days: int = 0
    # Debt tags that already embed finance-lease obligations; adding the
    # separately reported finance-lease liability on top would double-count.
    lease_inclusive_tags: frozenset[str] = frozenset()
    # False: mapped with provenance like any field, read by no metric. The
    # valuation plane's balance-sheet inputs (Hermes review of 02c2aac):
    # outside the candidate tables the scans iterate, outside the coverage
    # figure, never a silent revision on the card — a filer that reports no
    # preferred stock must read exactly as it did before these existed.
    scored: bool = True


def _g(*concepts: str) -> tuple[Tag, ...]:
    return tuple(("us-gaap", c) for c in concepts)


def _single(*tags: Tag) -> SeriesStrategy:
    return SeriesStrategy(Composition.SINGLE, Criterion.BEST_COVERAGE, tags=tags)


def _instant(name: str, *tags: Tag, **kw: Any) -> FieldSpec:
    return FieldSpec(name, Kind.INSTANT, (_single(*tags),), **kw)


def _flow(name: str, *strategies: SeriesStrategy, **kw: Any) -> FieldSpec:
    return FieldSpec(name, Kind.FLOW, strategies, **kw)


# dei share counts are stamped with cover dates, weeks after the quarter end.
COVER_DATE_TOLERANCE_DAYS = 60

# Short-term borrowings reported apart from the current portion of
# long-term debt. `DebtCurrent` is NOT one: it is aggregate current debt and
# has its own, mutually exclusive role.
_DEBT_SHORT = RoleSpec("short", _g("ShortTermBorrowings", "CommercialPaper"))
_FINANCE_LEASE_NC = RoleSpec("finance_lease_nc", _g("FinanceLeaseLiabilityNoncurrent"))
_FINANCE_LEASE_C = RoleSpec("finance_lease_c", _g("FinanceLeaseLiabilityCurrent"))

FIELDS: tuple[FieldSpec, ...] = (
    # --- instants (balance sheet) -------------------------------------------
    _instant("total_assets", *_g("Assets")),
    _instant("current_assets", *_g("AssetsCurrent")),
    _instant(
        "cash_and_equivalents",
        *_g(
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
    ),
    _instant(
        "receivables",
        *_g(
            "AccountsReceivableNetCurrent",
            "ReceivablesNetCurrent",
            "AccountsAndOtherReceivablesNetCurrent",
            "AccountsNotesAndLoansReceivableNetCurrent",
        ),
    ),
    _instant("inventory", *_g("InventoryNet", "InventoryGross")),
    _instant(
        "ppe_net",
        *_g(
            "PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        ),
    ),
    _instant(
        "intangible_assets",
        *_g("FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"),
    ),
    _instant("goodwill", *_g("Goodwill")),
    _instant("current_liabilities", *_g("LiabilitiesCurrent")),
    _instant(
        "accounts_payable",
        *_g(
            "AccountsPayableCurrent",
            "AccountsPayableAndAccruedLiabilitiesCurrent",
            "AccountsPayableTradeCurrent",
        ),
    ),
    _instant(
        "deferred_revenue",
        *_g("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"),
    ),
    _instant(
        "shares_outstanding",
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
        unit="shares",
        split_adjusted=True,
        cover_date_tolerance_days=COVER_DATE_TOLERANCE_DAYS,
    ),
    # --- flows (income and cash-flow statements) ----------------------------
    _flow(
        "revenue",
        _single(
            *_g(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet",
            )
        ),
    ),
    _flow(
        "cost_of_revenue",
        _single(*_g("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfSales")),
    ),
    _flow(
        "sga_expense",
        _single(*_g("SellingGeneralAndAdministrativeExpense")),
        # Used only when it covers strictly more quarters than the single tag.
        SeriesStrategy(
            Composition.SUM_ALL_REQUIRED,
            Criterion.BEST_COVERAGE,
            tags=_g("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense"),
        ),
    ),
    _flow("operating_income", _single(*_g("OperatingIncomeLoss"))),
    _flow("ebit", _single(*_g("OperatingIncomeLoss"))),
    _flow(
        "depreciation_amortization",
        # Aggregate concepts only: each already includes amortization, so
        # nothing is ever added on top of one.
        _single(
            *_g(
                "DepreciationDepletionAndAmortization",
                "DepreciationAmortizationAndAccretionNet",
                "DepreciationAndAmortization",
            )
        ),
        # Separately reported depreciation + amortization. Used when it
        # covers strictly more quarters than the aggregate.
        SeriesStrategy(
            Composition.SUM_ALL_REQUIRED,
            Criterion.BEST_COVERAGE,
            tags=_g("Depreciation", "AmortizationOfIntangibleAssets"),
        ),
        # Depreciation alone is PARTIAL D&A: used only when it covers strictly
        # more quarters than both of the above, and always noted as partial.
        # It was once an aggregate candidate, so 20 depreciation + 10
        # amortization mapped to 20.
        _single(*_g("Depreciation")),
    ),
    _flow(
        "interest_expense",
        _single(*_g("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt")),
    ),
    _flow("net_income", _single(*_g("NetIncomeLoss", "ProfitLoss"))),
    _flow(
        "stock_based_compensation",
        _single(*_g("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense")),
    ),
    _flow(
        "cfo",
        _single(
            *_g(
                "NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            )
        ),
    ),
    _flow(
        "capex",
        _single(
            *_g(
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "PaymentsToAcquireProductiveAssets",
                "PaymentsForCapitalImprovements",
            )
        ),
    ),
    _flow("buybacks", _single(*_g("PaymentsForRepurchaseOfCommonStock"))),
    _flow(
        "share_issuance_proceeds",
        _single(*_g("ProceedsFromIssuanceOfCommonStock", "ProceedsFromIssuanceOrSaleOfEquity")),
    ),
    _flow(
        "shares_diluted",
        _single(
            *_g(
                "WeightedAverageNumberOfDilutedSharesOutstanding",
                "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
            )
        ),
        unit="shares",
        split_adjusted=True,
        # Weighted-average counts do not sum across quarters.
        additive=False,
    ),
    # --- total debt: composed, never a single tag -----------------------------
    # Finance (capital) lease liabilities are a financing obligation and belong
    # in total debt (P0-10); operating-lease liabilities are deliberately
    # excluded. LongTermDebt is a TOTAL (current + noncurrent): the second
    # strategy, used only when the split is unavailable, never alongside it.
    FieldSpec(
        "total_debt",
        Kind.INSTANT,
        (
            SeriesStrategy(
                Composition.DEBT_BREAKDOWN,
                Criterion.FIRST_PRESENT_PER_DATE,
                roles=(
                    RoleSpec(
                        "noncurrent",
                        _g("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"),
                        required=True,
                    ),
                    # Aggregate current debt (current portion + short-term
                    # borrowings). When reported it IS the current side, and
                    # "current"/"short" are not added (double counting).
                    RoleSpec("current_aggregate", _g("DebtCurrent")),
                    RoleSpec(
                        "current",
                        _g("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
                    ),
                    _DEBT_SHORT,
                    _FINANCE_LEASE_NC,
                    _FINANCE_LEASE_C,
                ),
            ),
            SeriesStrategy(
                Composition.DEBT_BREAKDOWN,
                Criterion.FIRST_PRESENT_PER_DATE,
                roles=(
                    RoleSpec("total", _g("LongTermDebt"), required=True),
                    _DEBT_SHORT,
                    _FINANCE_LEASE_NC,
                    _FINANCE_LEASE_C,
                ),
            ),
        ),
        # `DebtCurrent` is "debt and lease obligation, classified as current"
        # in the US-GAAP taxonomy: a current finance-lease liability is not
        # added beside it.
        lease_inclusive_tags=frozenset(
            {
                "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligationsCurrent",
                "DebtCurrent",
            }
        ),
    ),
)

# --- valuation-plane inputs: mapped, never scored ---------------------------
# Read only by `app/services/valuation` (the EV bridge). Single concepts:
# the per-quarter strategy resolver and the debt composer are flow- and
# debt-specific, so a filer reporting only the operating-lease split
# (current + noncurrent) maps nothing here and the bridge line says so.
# Candidate order is the taxonomy's own aggregate first, then the concepts
# large filers use for the same line; the committed fixtures were trimmed
# to the registry before these existed and carry none of them.
_VALUATION_FIELDS: tuple[FieldSpec, ...] = (
    _instant(
        "short_term_investments",
        *_g(
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        ),
        scored=False,
    ),
    # The aggregate lessee liability only: shown beside EV, never in it.
    _instant("operating_lease_liabilities", *_g("OperatingLeaseLiability"), scored=False),
    _instant("minority_interest", *_g("MinorityInterest"), scored=False),
    _instant("preferred_stock", *_g("PreferredStockValue"), scored=False),
    # Parent-only equity first; the selection's label says which was read.
    _instant(
        "stockholders_equity",
        *_g(
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        scored=False,
    ),
)
FIELDS = FIELDS + _VALUATION_FIELDS

# Ordered: this is the order the "critical field missing" warnings render in.
CRITICAL_FIELDS: tuple[str, ...] = ("revenue", "net_income", "cfo", "total_assets")

_BY_NAME: dict[str, FieldSpec] = {f.name: f for f in FIELDS}


def field(name: str) -> FieldSpec:
    return _BY_NAME[name]


def unit_for(name: str) -> str:
    """A field's unit; names outside the registry are in `FILING_CURRENCY`."""
    spec = _BY_NAME.get(name)
    return spec.unit if spec is not None else FILING_CURRENCY


def is_scored(name: str) -> bool:
    """Whether a metric may read the field. A name outside the registry is
    held to the scored rules (inspected, counted), never waved through."""
    spec = _BY_NAME.get(name)
    return spec.scored if spec is not None else True


def scored_fields() -> tuple[str, ...]:
    """The fields the engine scores, in registry order."""
    return tuple(spec.name for spec in FIELDS if spec.scored)


def candidate_table(kind: Kind) -> dict[str, tuple[Tag, ...]]:
    """name -> single-tag candidates, for every SCORED field of `kind` that
    has a single-tag strategy, in registry order. Fields composed only from
    roles (total_debt) have no candidate list and are absent; so are the
    valuation plane's unscored fields, which the restatement scan and the
    vintage diff (the tables' readers) must not inspect."""
    out: dict[str, tuple[Tag, ...]] = {}
    for spec in FIELDS:
        if spec.kind is not kind or not spec.scored:
            continue
        for strategy in spec.strategies:
            if strategy.composition is Composition.SINGLE:
                out[spec.name] = strategy.tags
                break
    return out


def composite_components(name: str) -> tuple[Tag, ...]:
    """The summed components of a field's SUM_ALL_REQUIRED strategy."""
    for strategy in field(name).strategies:
        if strategy.composition is Composition.SUM_ALL_REQUIRED:
            return strategy.tags
    raise KeyError(f"{name} has no summed composite")


def partial_fallback(name: str) -> tuple[Tag, ...]:
    """Tags of a field's partial fallback: a SINGLE strategy listed after its
    first one (depreciation for D&A). Empty when the field has none."""
    singles = [s for s in field(name).strategies if s.composition is Composition.SINGLE]
    return singles[1].tags if len(singles) > 1 else ()


def role_tags(name: str, role: str) -> tuple[str, ...]:
    """Bare concept names for one role of a composed field — the first
    strategy that declares the role."""
    for strategy in field(name).strategies:
        for r in strategy.roles:
            if r.role == role:
                return tuple(concept for _, concept in r.candidates)
    raise KeyError(f"{name} has no role {role!r}")


def all_tags() -> frozenset[Tag]:
    """Every (taxonomy, concept) a SCORED field may be built from: each
    strategy's tags and every role's candidates. The single answer to "which
    concepts does the score read" — trimming a payload to anything less makes
    the trimmed dataset's scored figures diverge from the live one. The
    point-in-time path (`backtesting/pit.py`, flag-only) trims to exactly
    this set, which the valuation plane's unscored fields do not move; a
    payload trimmed to it maps those fields as missing (`every_tag`)."""
    return frozenset(
        t for spec in FIELDS if spec.scored for s in spec.strategies for t in s.all_tags()
    )


def every_tag() -> frozenset[Tag]:
    """`all_tags` plus the unscored (valuation-plane) fields' concepts: what a
    fixture must keep for the EV bridge to find its balance-sheet lines."""
    return frozenset(t for spec in FIELDS for s in spec.strategies for t in s.all_tags())
