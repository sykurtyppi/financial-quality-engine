"""Capital-structure and dilution metrics (SBC burden, dilution, buyback offset)."""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult
from app.services.formulas.base import build_metric

# Fraction of the operating cash burn that equity issuance must cover before
# a negative-CFO quarter is treated as external funding dependence (review
# finding 7). Documented HEURISTIC — economically motivated, not calibrated.
MATERIAL_ISSUANCE_COVERAGE = 0.25


def sbc_to_revenue(cur: PeriodFinancials) -> MetricResult:
    inputs = {"stock_based_compensation": cur.stock_based_compensation, "revenue": cur.revenue}
    return build_metric(
        "sbc_to_revenue",
        "SBC / Revenue",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive revenue" if cur.revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.stock_based_compensation / cur.revenue,  # type: ignore[operator]
    )


def sbc_to_cfo(cur: PeriodFinancials) -> MetricResult:
    inputs = {"stock_based_compensation": cur.stock_based_compensation, "cfo": cur.cfo}
    return build_metric(
        "sbc_to_cfo",
        "SBC / CFO",
        cur.fiscal_label,
        inputs,
        guard=lambda: (
            "Non-positive CFO: SBC/CFO not meaningful; SBC exceeds all operating cash"
            if cur.cfo <= 0  # type: ignore[operator]
            else None
        ),
        value_fn=lambda: cur.stock_based_compensation / cur.cfo,  # type: ignore[operator]
    )


def diluted_share_growth(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {"shares_diluted": cur.shares_diluted, "shares_diluted_prior": prev.shares_diluted}
    return build_metric(
        "diluted_share_growth",
        "(DilutedShares_t - DilutedShares_t-1) / DilutedShares_t-1",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive prior share count" if prev.shares_diluted <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.shares_diluted - prev.shares_diluted) / prev.shares_diluted,  # type: ignore[operator]
    )


def net_share_count_change(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "shares_outstanding": cur.shares_outstanding,
        "shares_outstanding_prior": prev.shares_outstanding,
    }
    return build_metric(
        "net_share_count_change",
        "(SharesOut_t - SharesOut_t-1) / SharesOut_t-1",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive prior share count" if prev.shares_outstanding <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.shares_outstanding - prev.shares_outstanding)  # type: ignore[operator]
        / prev.shares_outstanding,  # type: ignore[operator]
    )


def buyback_offset_ratio(cur: PeriodFinancials) -> MetricResult:
    """Buybacks / SBC. Values near or below 1.0 while buybacks are marketed as
    shareholder returns indicate repurchases mostly offset compensation dilution."""
    inputs = {"buybacks": cur.buybacks, "stock_based_compensation": cur.stock_based_compensation}
    return build_metric(
        "buyback_offset_ratio",
        "Buybacks / SBC",
        cur.fiscal_label,
        inputs,
        guard=lambda: (
            "Zero SBC: offset ratio undefined"
            if cur.stock_based_compensation == 0
            else None
        ),
        value_fn=lambda: cur.buybacks / cur.stock_based_compensation,  # type: ignore[operator]
    )


def issuance_pressure(cur: PeriodFinancials) -> MetricResult:
    """Share issuance proceeds / CFO: dependence on equity financing relative
    to operating cash generation."""
    inputs = {"share_issuance_proceeds": cur.share_issuance_proceeds, "cfo": cur.cfo}
    return build_metric(
        "issuance_pressure",
        "Issuance Proceeds / CFO",
        cur.fiscal_label,
        inputs,
        # P0-9: an actual operating cash BURN funded by material equity issuance
        # is external funding dependence. Review findings 5 & 7 (both rounds):
        #  - cfo must be strictly NEGATIVE (a genuine burn); cfo == 0 is
        #    breakeven, not distress, and previously made the relative threshold
        #    0.25*|0| == 0 so zero issuance fired (the zero/zero bug).
        #  - issuance must be strictly POSITIVE and cover >= MATERIAL_ISSUANCE_
        #    COVERAGE of the burn, so a token amount does not fire.
        # NOTE: the 25% coverage threshold is a documented HEURISTIC on frozen
        # scoring and still needs before/after validation + human approval before
        # it can be considered calibrated.
        distress_guard=lambda: (
            "Operating cash burn funded by material equity issuance: external funding dependence"
            if cur.cfo < 0  # type: ignore[operator]
            and cur.share_issuance_proceeds > 0  # type: ignore[operator]
            and cur.share_issuance_proceeds >= MATERIAL_ISSUANCE_COVERAGE * abs(cur.cfo)  # type: ignore[operator]
            else None
        ),
        value_fn=lambda: cur.share_issuance_proceeds / cur.cfo,  # type: ignore[operator]
    )
