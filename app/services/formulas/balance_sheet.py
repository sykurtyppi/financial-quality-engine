"""Balance-sheet stress and asset-quality metrics."""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult
from app.services.formulas.base import build_metric


def net_debt_to_ebitda(cur: PeriodFinancials) -> MetricResult:
    inputs = {
        "total_debt": cur.total_debt,
        "cash_and_equivalents": cur.cash_and_equivalents,
        "ebit": cur.ebit,
        "depreciation_amortization": cur.depreciation_amortization,
    }

    def distress_guard() -> str | None:
        ebitda = cur.ebit + cur.depreciation_amortization  # type: ignore[operator]
        net_debt = cur.total_debt - cur.cash_and_equivalents  # type: ignore[operator]
        if ebitda <= 0 and net_debt > 0:
            # Negative EBITDA while carrying net debt: leverage cannot be
            # covered by earnings at all — maximal leverage concern (P0-9).
            return "Non-positive EBITDA with net debt: leverage uncoverable; maximum concern"
        return None

    def guard() -> str | None:
        ebitda = cur.ebit + cur.depreciation_amortization  # type: ignore[operator]
        if ebitda <= 0:
            # Reaches here only with net cash: a temporary loss at a debt-free /
            # net-cash balance sheet is genuinely not a leverage concern.
            return "Non-positive EBITDA with net cash: leverage multiple not meaningful"
        return None

    return build_metric(
        "net_debt_to_ebitda",
        "(Total Debt - Cash) / (EBIT + D&A)",
        cur.fiscal_label,
        inputs,
        guard=guard,
        distress_guard=distress_guard,
        value_fn=lambda: (cur.total_debt - cur.cash_and_equivalents)  # type: ignore[operator]
        / (cur.ebit + cur.depreciation_amortization),  # type: ignore[operator]
    )


def interest_coverage(cur: PeriodFinancials) -> MetricResult:
    inputs = {"ebit": cur.ebit, "interest_expense": cur.interest_expense}

    def guard() -> str | None:
        if cur.interest_expense == 0:
            return "Zero interest expense: coverage undefined (no leverage cost)"
        if cur.interest_expense < 0:  # type: ignore[operator]
            return "Negative interest expense input; check sign convention"
        return None

    return build_metric(
        "interest_coverage",
        "EBIT / Interest Expense",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: cur.ebit / cur.interest_expense,  # type: ignore[operator]
    )


def debt_to_assets(cur: PeriodFinancials) -> MetricResult:
    inputs = {"total_debt": cur.total_debt, "total_assets": cur.total_assets}
    return build_metric(
        "debt_to_assets",
        "Total Debt / Total Assets",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive total assets" if cur.total_assets <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.total_debt / cur.total_assets,  # type: ignore[operator]
    )


def current_ratio(cur: PeriodFinancials) -> MetricResult:
    inputs = {"current_assets": cur.current_assets, "current_liabilities": cur.current_liabilities}
    return build_metric(
        "current_ratio",
        "Current Assets / Current Liabilities",
        cur.fiscal_label,
        inputs,
        # Round-14 finding 1: `== 0` used to let negative current_liabilities
        # through (rare — negative accruals, over-refunded advances). The
        # resulting ratio (e.g. 200 / -10 = -20) then clamped to max concern
        # (85/100) for what is usually a benign reclassification. Aligning with
        # the `<= 0` convention used by every other denominator guard here.
        # (base.build_metric already handles the None case via MISSING_DATA.)
        guard=lambda: "Non-positive current liabilities" if cur.current_liabilities <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.current_assets / cur.current_liabilities,  # type: ignore[operator]
    )


def asset_quality_proxy(cur: PeriodFinancials) -> MetricResult:
    """Share of assets that are neither current assets nor PP&E ("soft assets").
    Rising soft-asset share can indicate capitalization of costs."""
    inputs = {
        "current_assets": cur.current_assets,
        "ppe_net": cur.ppe_net,
        "total_assets": cur.total_assets,
    }
    return build_metric(
        "asset_quality_proxy",
        "1 - (Current Assets + PP&E) / Total Assets",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive total assets" if cur.total_assets <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: 1.0 - (cur.current_assets + cur.ppe_net) / cur.total_assets,  # type: ignore[operator]
    )


def intangibles_to_assets(cur: PeriodFinancials) -> MetricResult:
    inputs = {
        "intangible_assets": cur.intangible_assets,
        "goodwill": cur.goodwill,
        "total_assets": cur.total_assets,
    }
    return build_metric(
        "intangibles_to_assets",
        "(Intangibles + Goodwill) / Total Assets",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive total assets" if cur.total_assets <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.intangible_assets + cur.goodwill) / cur.total_assets,  # type: ignore[operator]
    )


def goodwill_growth(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    """Goodwill growth rate. Interpreting whether growth matches disclosed
    acquisition activity requires document evidence; this metric supplies the
    quantitative side only."""
    inputs = {"goodwill": cur.goodwill, "goodwill_prior": prev.goodwill}
    return build_metric(
        "goodwill_growth",
        "(Goodwill_t - Goodwill_t-1) / Goodwill_t-1",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Zero or negative prior goodwill" if prev.goodwill <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.goodwill - prev.goodwill) / prev.goodwill,  # type: ignore[operator]
    )


def leverage_change(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "total_debt": cur.total_debt,
        "total_assets": cur.total_assets,
        "total_debt_prior": prev.total_debt,
        "total_assets_prior": prev.total_assets,
    }

    def guard() -> str | None:
        if cur.total_assets <= 0 or prev.total_assets <= 0:  # type: ignore[operator]
            return "Non-positive total assets"
        return None

    return build_metric(
        "leverage_change",
        "Debt_t/Assets_t - Debt_t-1/Assets_t-1",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: cur.total_debt / cur.total_assets  # type: ignore[operator]
        - prev.total_debt / prev.total_assets,  # type: ignore[operator]
    )
