"""Beneish M-score components (Beneish 1999).

Presentation note: the M-score is reported as an "earnings quality screen"
input, never as a manipulation accusation. See docs/legal_framing.md.

Implementation notes / documented deviations:
- LVGI leverage uses (total_debt + current_liabilities) / total_assets because
  the canonical (LTD + CL) split is not always available in normalized data;
  when total_debt already includes short-term debt this slightly double-counts
  any short-term debt inside current_liabilities. Flagged in formula_spec.
- TATA uses (Net Income - CFO) / Total Assets (the common post-SFAS-95 form)
  rather than the original balance-sheet delta form.
- The model was estimated on annual data; applying it to quarterly series is a
  screening convenience and is noted as a caveat by the scoring layer.
"""

from __future__ import annotations

from app.schemas.financials import PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.base import build_metric

M_SCORE_WEIGHTS = {
    "const": -4.84,
    "dsri": 0.920,
    "gmi": 0.528,
    "aqi": 0.404,
    "sgi": 0.892,
    "depi": 0.115,
    "sgai": -0.172,
    "tata": 4.679,
    "lvgi": -0.327,
}


def dsri(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "receivables": cur.receivables,
        "revenue": cur.revenue,
        "receivables_prior": prev.receivables,
        "revenue_prior": prev.revenue,
    }

    def guard() -> str | None:
        if cur.revenue == 0 or prev.revenue == 0 or prev.receivables == 0:
            return "Zero revenue or prior receivables"
        if cur.revenue < 0 or prev.revenue < 0:  # type: ignore[operator]
            return "Negative revenue"
        return None

    return build_metric(
        "beneish_dsri",
        "(Receivables_t / Revenue_t) / (Receivables_t-1 / Revenue_t-1)",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: (cur.receivables / cur.revenue)  # type: ignore[operator]
        / (prev.receivables / prev.revenue),  # type: ignore[operator]
    )


def gmi(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "revenue": cur.revenue,
        "cost_of_revenue": cur.cost_of_revenue,
        "revenue_prior": prev.revenue,
        "cost_of_revenue_prior": prev.cost_of_revenue,
    }

    def guard() -> str | None:
        if cur.revenue == 0 or prev.revenue == 0:
            return "Zero revenue"
        gm_cur = (cur.revenue - cur.cost_of_revenue) / cur.revenue  # type: ignore[operator]
        if gm_cur == 0:
            return "Zero current gross margin"
        return None

    return build_metric(
        "beneish_gmi",
        "GrossMargin_t-1 / GrossMargin_t",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: ((prev.revenue - prev.cost_of_revenue) / prev.revenue)  # type: ignore[operator]
        / ((cur.revenue - cur.cost_of_revenue) / cur.revenue),  # type: ignore[operator]
    )


def aqi(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "current_assets": cur.current_assets,
        "ppe_net": cur.ppe_net,
        "total_assets": cur.total_assets,
        "current_assets_prior": prev.current_assets,
        "ppe_net_prior": prev.ppe_net,
        "total_assets_prior": prev.total_assets,
    }

    def soft_assets(p: PeriodFinancials) -> float:
        return 1.0 - (p.current_assets + p.ppe_net) / p.total_assets  # type: ignore[operator]

    def guard() -> str | None:
        if cur.total_assets == 0 or prev.total_assets == 0:
            return "Zero total assets"
        if soft_assets(prev) == 0:
            return "Prior soft-asset share is zero"
        return None

    return build_metric(
        "beneish_aqi",
        "[1 - (CA_t + PPE_t)/TA_t] / [1 - (CA_t-1 + PPE_t-1)/TA_t-1]",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: soft_assets(cur) / soft_assets(prev),
    )


def sgi(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {"revenue": cur.revenue, "revenue_prior": prev.revenue}
    return build_metric(
        "beneish_sgi",
        "Revenue_t / Revenue_t-1",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive prior revenue" if prev.revenue <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: cur.revenue / prev.revenue,  # type: ignore[operator]
    )


def depi(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "depreciation_amortization": cur.depreciation_amortization,
        "ppe_net": cur.ppe_net,
        "depreciation_amortization_prior": prev.depreciation_amortization,
        "ppe_net_prior": prev.ppe_net,
    }

    def rate(p: PeriodFinancials) -> float:
        return p.depreciation_amortization / (p.depreciation_amortization + p.ppe_net)  # type: ignore[operator]

    def guard() -> str | None:
        for p in (cur, prev):
            if (p.depreciation_amortization + p.ppe_net) == 0:  # type: ignore[operator]
                return "Zero D&A + PP&E base"
        if rate(cur) == 0:
            return "Zero current depreciation rate"
        return None

    return build_metric(
        "beneish_depi",
        "DeprRate_t-1 / DeprRate_t, DeprRate = D&A / (D&A + PP&E)",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: rate(prev) / rate(cur),
    )


def sgai(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "sga_expense": cur.sga_expense,
        "revenue": cur.revenue,
        "sga_expense_prior": prev.sga_expense,
        "revenue_prior": prev.revenue,
    }

    def guard() -> str | None:
        if cur.revenue == 0 or prev.revenue == 0 or prev.sga_expense == 0:
            return "Zero revenue or prior SG&A"
        return None

    return build_metric(
        "beneish_sgai",
        "(SGA_t / Revenue_t) / (SGA_t-1 / Revenue_t-1)",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: (cur.sga_expense / cur.revenue)  # type: ignore[operator]
        / (prev.sga_expense / prev.revenue),  # type: ignore[operator]
    )


def lvgi(cur: PeriodFinancials, prev: PeriodFinancials) -> MetricResult:
    inputs = {
        "total_debt": cur.total_debt,
        "current_liabilities": cur.current_liabilities,
        "total_assets": cur.total_assets,
        "total_debt_prior": prev.total_debt,
        "current_liabilities_prior": prev.current_liabilities,
        "total_assets_prior": prev.total_assets,
    }

    def lev(p: PeriodFinancials) -> float:
        return (p.total_debt + p.current_liabilities) / p.total_assets  # type: ignore[operator]

    def guard() -> str | None:
        if cur.total_assets == 0 or prev.total_assets == 0:
            return "Zero total assets"
        if lev(prev) == 0:
            return "Zero prior leverage"
        return None

    return build_metric(
        "beneish_lvgi",
        "[(Debt_t + CL_t)/TA_t] / [(Debt_t-1 + CL_t-1)/TA_t-1]",
        cur.fiscal_label,
        inputs,
        guard=guard,
        value_fn=lambda: lev(cur) / lev(prev),
    )


def tata(cur: PeriodFinancials) -> MetricResult:
    inputs = {
        "net_income": cur.net_income,
        "cfo": cur.cfo,
        "total_assets": cur.total_assets,
    }
    return build_metric(
        "beneish_tata",
        "(Net Income - CFO) / Total Assets",
        cur.fiscal_label,
        inputs,
        guard=lambda: "Non-positive total assets" if cur.total_assets <= 0 else None,  # type: ignore[operator]
        value_fn=lambda: (cur.net_income - cur.cfo) / cur.total_assets,  # type: ignore[operator]
    )


def m_score(components: dict[str, MetricResult], fiscal_label: str) -> MetricResult:
    """Weighted 8-component M-score. Requires all components OK; a partial
    M-score would be silently wrong, so missing components propagate."""
    expected = ["dsri", "gmi", "aqi", "sgi", "depi", "sgai", "tata", "lvgi"]
    missing = [
        k
        for k in expected
        if k not in components or components[k].status is not MetricStatus.OK
    ]
    formula = (
        "-4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI "
        "- 0.172*SGAI + 4.679*TATA - 0.327*LVGI"
    )
    if missing:
        return MetricResult(
            name="beneish_m_score",
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.MISSING_DATA,
            missing_fields=[f"component:{k}" for k in missing],
            note="M-score requires all 8 components; partial computation would be misleading.",
        )
    value = M_SCORE_WEIGHTS["const"] + sum(
        M_SCORE_WEIGHTS[k] * components[k].value  # type: ignore[operator]
        for k in expected
    )
    return MetricResult(
        name="beneish_m_score",
        formula=formula,
        fiscal_label=fiscal_label,
        status=MetricStatus.OK,
        value=value,
        inputs={k: components[k].value for k in expected},
        note="Estimated on annual data; quarterly application is a screening convenience.",
    )


def compute_all(cur: PeriodFinancials, prev: PeriodFinancials) -> list[MetricResult]:
    components = {
        "dsri": dsri(cur, prev),
        "gmi": gmi(cur, prev),
        "aqi": aqi(cur, prev),
        "sgi": sgi(cur, prev),
        "depi": depi(cur, prev),
        "sgai": sgai(cur, prev),
        "tata": tata(cur),
        "lvgi": lvgi(cur, prev),
    }
    results = list(components.values())
    results.append(m_score(components, cur.fiscal_label))
    return results
