"""TTM annualization (roadmap P0-A).

Stock-over-flow ratios and annually-anchored models must never consume a single
quarter's flow: 668 of 2,000 sampled credit agreements define leverage on
4-quarter EBITDA and none use a single quarter, and the Beneish/Sloan anchor
vocabulary is annual. The measured failure this fixes: net_debt_to_ebitda
overstated ~4x on a quarterly denominator (GLW 7.39x vs ~1.85x true).

`annualize` builds a synthetic annual-basis PeriodFinancials ending at a given
quarter: flow fields are summed over the 4 consecutive quarters, instant fields
(balance sheet, shares) are taken from the ending quarter. Existing formulas
then run unchanged on annual-scale inputs, and year-over-year pairs
(annualize(i), annualize(i-4)) give Beneish its intended comparison basis.
"""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials, PeriodType

# A 13-week quarter spans ~91 days; a 53-week fiscal year contains one 14-week
# (98-day) quarter. The bounds accept both while rejecting a skipped quarter.
MIN_GAP_DAYS = 75
MAX_GAP_DAYS = 105

TTM_LABEL_PREFIX = "TTM "

FLOW_FIELDS: tuple[str, ...] = (
    "revenue",
    "cost_of_revenue",
    "sga_expense",
    "operating_income",
    "ebit",
    "depreciation_amortization",
    "interest_expense",
    "net_income",
    "stock_based_compensation",
    "cfo",
    "capex",
    "buybacks",
    "share_issuance_proceeds",
)

TTM_WINDOW_MISSING = "ttm_window (4 consecutive quarters)"
PRIOR_YEAR_MISSING = "prior_year_quarter (t-4)"


def annualize(periods: list[PeriodFinancials], idx: int) -> PeriodFinancials | None:
    """Annual-basis view ending at periods[idx], or None if no valid window.

    A flow field is None when it is missing in ANY quarter of the window —
    a partial sum would silently understate the year, which is worse than an
    explicit gap.
    """
    if idx < 3 or idx >= len(periods):
        return None
    window = periods[idx - 3 : idx + 1]
    if any(p.period_type is not PeriodType.QUARTER for p in window):
        return None
    for a, b in zip(window, window[1:]):
        gap = (b.period_end - a.period_end).days
        if not (MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS):
            return None

    end = window[-1]
    data = end.model_dump()
    for field in FLOW_FIELDS:
        values = [getattr(p, field) for p in window]
        data[field] = None if any(v is None for v in values) else sum(values)
    data["fiscal_label"] = f"{TTM_LABEL_PREFIX}{end.fiscal_label}"
    return PeriodFinancials(**data)
