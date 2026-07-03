"""Deterministic fixture companies.

CLEANCO: steady grower with strong cash conversion, low SBC, buybacks
exceeding SBC, stable working capital, and unremarkable disclosure language.

STRETCHCO: slow grower with receivables outpacing revenue, deteriorating cash
conversion, rising SBC and dilution, accelerating capex, recurring
"one-time" restructuring language, and a KPI quietly dropped in the latest
period.
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    DocumentRecord,
    DocumentType,
    PeriodFinancials,
    PeriodType,
)

QUARTER_ENDS = [
    date(2024, 3, 31),
    date(2024, 6, 30),
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
]
LABELS = [
    "FY2024Q1", "FY2024Q2", "FY2024Q3", "FY2024Q4",
    "FY2025Q1", "FY2025Q2", "FY2025Q3", "FY2025Q4",
]


def clean_period(i: int) -> PeriodFinancials:
    rev = 1000.0 * (1.03**i)
    cogs = rev * 0.55
    ni = rev * 0.12
    sbc = rev * 0.03
    return PeriodFinancials(
        period_end=QUARTER_ENDS[i],
        period_type=PeriodType.QUARTER,
        fiscal_label=LABELS[i],
        revenue=rev,
        cost_of_revenue=cogs,
        sga_expense=rev * 0.20,
        operating_income=rev * 0.18,
        ebit=rev * 0.18,
        depreciation_amortization=40.0 + i,
        interest_expense=5.0,
        net_income=ni,
        stock_based_compensation=sbc,
        cfo=ni * 1.25,
        capex=rev * 0.05,
        buybacks=sbc * 2.5,
        share_issuance_proceeds=0.0,
        total_assets=4000.0 + 100.0 * i,
        current_assets=1500.0 + 40.0 * i,
        cash_and_equivalents=500.0,
        receivables=rev * 0.50,
        inventory=cogs * 0.60,
        ppe_net=1200.0 + 20.0 * i,
        intangible_assets=100.0,
        goodwill=200.0,
        total_debt=800.0,
        current_liabilities=700.0 + 5.0 * i,
        accounts_payable=cogs * 0.50,
        deferred_revenue=rev * 0.30,
        shares_diluted=100.0 - 0.3 * i,
        shares_outstanding=99.0 - 0.3 * i,
    )


def stretch_period(i: int) -> PeriodFinancials:
    rev = 1000.0 * (1.02**i)
    cogs = rev * 0.58
    ni = rev * 0.10
    sbc = rev * (0.06 + 0.01 * i)
    return PeriodFinancials(
        period_end=QUARTER_ENDS[i],
        period_type=PeriodType.QUARTER,
        fiscal_label=LABELS[i],
        revenue=rev,
        cost_of_revenue=cogs,
        sga_expense=rev * 0.24,
        operating_income=rev * 0.12,
        ebit=rev * 0.12,
        depreciation_amortization=40.0,
        interest_expense=12.0 + i,
        net_income=ni,
        stock_based_compensation=sbc,
        cfo=ni * (1.10 - 0.15 * i),
        capex=rev * 0.04 * (1.0 + 0.25 * i),
        buybacks=sbc * 0.5,
        share_issuance_proceeds=10.0 + 2.0 * i,
        total_assets=4000.0 + 150.0 * i,
        current_assets=1500.0 + 70.0 * i,
        cash_and_equivalents=400.0 - 20.0 * i,
        receivables=rev * 0.50 * (1.35**i),
        inventory=cogs * 0.60 * (1.0 + 0.05 * i),
        ppe_net=1200.0 + 60.0 * i,
        intangible_assets=300.0 + 10.0 * i,
        goodwill=600.0 + 30.0 * i,
        total_debt=800.0 + 40.0 * i,
        current_liabilities=700.0 + 20.0 * i,
        accounts_payable=cogs * 0.50,
        deferred_revenue=rev * 0.30 * (1.0 - 0.03 * i),
        shares_diluted=100.0 * (1.01**i),
        shares_outstanding=99.0 * (1.01**i),
    )


_CLEAN_DOC = (
    "Revenue grew in line with our expectations this quarter, driven by continued "
    "customer demand. Gross margin remained stable and free cash flow was strong. "
    "Net revenue retention remained above 110 percent and total paying customers "
    "grew steadily. We continue to invest in the platform with discipline."
)

_STRETCH_DOC_EARLY = (
    "Results this quarter include restructuring charges related to our transformation "
    "program, which we believe are one-time in nature. Adjusted EBITDA excludes these "
    "non-recurring costs and impairment charges. Net revenue retention was 108 percent "
    "and remaining performance obligations grew. Our optimization initiatives continue."
)

_STRETCH_DOC_LATE = (
    "Results this quarter include restructuring charges related to our transformation "
    "program, which we believe are one-time in nature. Adjusted EBITDA excludes these "
    "non-recurring costs and impairment charges. Our optimization initiatives continue "
    "to position the business for long-term efficiency."
)


def clean_dataset() -> CompanyDataset:
    return CompanyDataset(
        profile=CompanyProfile(ticker="CLEANCO", name="Clean Co", sector="Industrials"),
        periods=[clean_period(i) for i in range(8)],
        documents=[
            DocumentRecord(
                fiscal_label=LABELS[i],
                doc_type=DocumentType.EARNINGS_RELEASE,
                text=_CLEAN_DOC,
            )
            for i in range(4, 8)
        ],
    )


def stretch_dataset() -> CompanyDataset:
    docs = [
        DocumentRecord(
            fiscal_label=LABELS[i],
            doc_type=DocumentType.EARNINGS_RELEASE,
            text=_STRETCH_DOC_EARLY,
        )
        for i in range(4, 7)
    ]
    # Latest period drops "net revenue retention" and "RPO" — KPI removal.
    docs.append(
        DocumentRecord(
            fiscal_label=LABELS[7],
            doc_type=DocumentType.EARNINGS_RELEASE,
            text=_STRETCH_DOC_LATE,
        )
    )
    return CompanyDataset(
        profile=CompanyProfile(ticker="STRETCHCO", name="Stretch Co", sector="Technology"),
        periods=[stretch_period(i) for i in range(8)],
        documents=docs,
    )
