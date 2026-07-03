"""Canonical normalized financial statement data for one reporting period.

All monetary fields are Optional: missing data is a first-class state that
must be surfaced, never silently imputed. Values are in the filer's reporting
currency, unscaled (i.e. actual units, not thousands/millions).
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class PeriodType(str, Enum):
    QUARTER = "Q"
    ANNUAL = "FY"


class CompanyProfile(BaseModel):
    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    is_financial_institution: bool = Field(
        default=False,
        description=(
            "Banks, insurers, and other financial institutions are excluded: "
            "accrual- and working-capital-based formulas are not meaningful "
            "for their balance sheets."
        ),
    )


class PeriodFinancials(BaseModel):
    """One period of normalized statement data.

    Field names follow common XBRL/us-gaap concepts so an EDGAR adapter can
    map onto them directly.
    """

    period_end: date
    period_type: PeriodType
    fiscal_label: str = Field(description='e.g. "FY2025Q3" or "FY2025"')

    # Income statement
    revenue: float | None = None
    cost_of_revenue: float | None = None
    sga_expense: float | None = None
    operating_income: float | None = None
    ebit: float | None = None
    depreciation_amortization: float | None = None
    interest_expense: float | None = None
    net_income: float | None = None
    stock_based_compensation: float | None = None

    # Cash flow statement
    cfo: float | None = Field(default=None, description="Net cash from operating activities")
    capex: float | None = Field(default=None, description="Purchases of PP&E (positive number)")
    buybacks: float | None = Field(default=None, description="Common stock repurchased (positive number)")
    share_issuance_proceeds: float | None = None

    # Balance sheet
    total_assets: float | None = None
    current_assets: float | None = None
    cash_and_equivalents: float | None = None
    receivables: float | None = None
    inventory: float | None = None
    ppe_net: float | None = None
    intangible_assets: float | None = Field(default=None, description="Intangibles excluding goodwill")
    goodwill: float | None = None
    total_debt: float | None = None
    current_liabilities: float | None = None
    accounts_payable: float | None = None
    deferred_revenue: float | None = None

    # Shares
    shares_diluted: float | None = None
    shares_outstanding: float | None = None

    @property
    def ebitda(self) -> float | None:
        if self.ebit is not None and self.depreciation_amortization is not None:
            return self.ebit + self.depreciation_amortization
        return None

    @property
    def fcf(self) -> float | None:
        if self.cfo is not None and self.capex is not None:
            return self.cfo - self.capex
        return None

    @property
    def gross_profit(self) -> float | None:
        if self.revenue is not None and self.cost_of_revenue is not None:
            return self.revenue - self.cost_of_revenue
        return None


class DocumentType(str, Enum):
    EARNINGS_RELEASE = "earnings_release"
    TRANSCRIPT = "transcript"
    MDNA = "mdna"
    TEN_K = "10-K"
    TEN_Q = "10-Q"


class DocumentRecord(BaseModel):
    """A source document tied to a reporting period, used by the narrative engine."""

    fiscal_label: str
    doc_type: DocumentType
    text: str
    source: str | None = Field(default=None, description="URL or filing accession number")


class CompanyDataset(BaseModel):
    """Full analysis input: profile + ordered period series + documents."""

    profile: CompanyProfile
    periods: list[PeriodFinancials] = Field(
        description="Ascending chronological order (oldest first)"
    )
    documents: list[DocumentRecord] = Field(default_factory=list)

    def sorted_periods(self) -> list[PeriodFinancials]:
        return sorted(self.periods, key=lambda p: p.period_end)
