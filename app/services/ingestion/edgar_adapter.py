"""EdgarTools adapter: builds a CompanyDataset from SEC EDGAR XBRL facts.

STATUS: functional mapping code, but NETWORK-DEPENDENT and NOT covered by the
offline test suite. Requires the optional `edgar` extra:

    pip install "financial-quality-engine[edgar]"

Known limitations (deliberate, documented rather than hidden):
- Concept mapping covers the common us-gaap tags only; filers using custom
  extension tags for mapped concepts will surface as missing data (which the
  engine reports explicitly downstream).
- total_debt is approximated as short-term + long-term debt tags when a
  combined concept is unavailable.
- Documents (transcripts, releases) are NOT fetched here; supply them via the
  canonical JSON format.
"""

from __future__ import annotations

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)

# Canonical field -> ordered candidate us-gaap concepts (first hit wins).
CONCEPT_MAP: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "sga_expense": ("SellingGeneralAndAdministrativeExpense",),
    "operating_income": ("OperatingIncomeLoss",),
    "ebit": ("OperatingIncomeLoss",),
    "depreciation_amortization": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
    "interest_expense": ("InterestExpense", "InterestExpenseDebt"),
    "net_income": ("NetIncomeLoss",),
    "stock_based_compensation": ("ShareBasedCompensation",),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "share_issuance_proceeds": ("ProceedsFromIssuanceOfCommonStock",),
    "total_assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "receivables": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "inventory": ("InventoryNet",),
    "ppe_net": ("PropertyPlantAndEquipmentNet",),
    "intangible_assets": ("FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"),
    "goodwill": ("Goodwill",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "accounts_payable": ("AccountsPayableCurrent",),
    "deferred_revenue": ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "shares_outstanding": ("CommonStockSharesOutstanding",),
}

DEBT_CONCEPTS: tuple[str, ...] = (
    "LongTermDebtNoncurrent",
    "LongTermDebtCurrent",
    "ShortTermBorrowings",
    "DebtCurrent",
)


def fetch_dataset(ticker: str, n_quarters: int = 8) -> CompanyDataset:
    """Fetch and map quarterly fundamentals for `ticker` from EDGAR.

    Raises RuntimeError if edgartools is not installed.
    """
    try:
        from edgar import Company, set_identity  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "edgartools is not installed. Install the optional extra: "
            'pip install "financial-quality-engine[edgar]" and set '
            "EDGAR_IDENTITY per SEC fair-access rules."
        ) from e

    import os

    identity = os.environ.get("EDGAR_IDENTITY")
    if identity:
        set_identity(identity)

    company = Company(ticker)
    facts = company.get_facts()
    periods: list[PeriodFinancials] = []

    # edgartools exposes quarterly statement data via facts; iterate periods.
    quarterly = facts.to_pandas() if facts is not None else None
    if quarterly is None or quarterly.empty:
        raise RuntimeError(f"No XBRL facts returned for {ticker}")

    by_period: dict = {}
    for _, row in quarterly.iterrows():
        end = row.get("end")
        concept = row.get("fact") or row.get("namespace_fact") or row.get("concept")
        value = row.get("value")
        if end is None or concept is None or value is None:
            continue
        by_period.setdefault(end, {})[str(concept)] = value

    for end_date in sorted(by_period)[-n_quarters:]:
        concepts = by_period[end_date]

        def pick(field: str) -> float | None:
            for tag in CONCEPT_MAP.get(field, ()):
                if tag in concepts:
                    try:
                        return float(concepts[tag])
                    except (TypeError, ValueError):
                        return None
            return None

        debt_parts = [float(concepts[t]) for t in DEBT_CONCEPTS if t in concepts]
        kwargs = {field: pick(field) for field in CONCEPT_MAP}
        kwargs["total_debt"] = sum(debt_parts) if debt_parts else None
        periods.append(
            PeriodFinancials(
                period_end=end_date,
                period_type=PeriodType.QUARTER,
                fiscal_label=f"P{end_date}",
                **kwargs,
            )
        )

    return CompanyDataset(
        profile=CompanyProfile(ticker=ticker.upper()),
        periods=periods,
    )
