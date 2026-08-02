"""End-to-end deterministic analysis pipeline.

Order is enforced here: formulas and narrative metrics are computed BEFORE any
interpretation (flags, changes, questions) is generated, and every generated
statement references the metrics that back it.
"""

from __future__ import annotations

from app.config import scoring_config as cfg
from app.schemas.financials import CompanyDataset
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import AnalysisResult, EvidenceEntry, Flag
from app.services.formulas.registry import MetricsBundle, compute_metrics
from app.services.narrative.narrative_metrics import compute_narrative_layer
from app.services.scoring.engine import interpolate_concern, score_all


def _financial_concerns(metrics_by_name: dict[str, MetricResult]) -> dict[str, float]:
    """Concern scores for OK financial metrics, straight from the config
    anchors — used by the narrative layer's mismatch checks before full
    scoring runs."""
    concerns: dict[str, float] = {}
    for block in cfg.BLOCKS:
        for ms in block.metrics:
            m = metrics_by_name.get(ms.metric_name)
            if m is not None and m.status is MetricStatus.OK and m.value is not None:
                concerns.setdefault(ms.metric_name, interpolate_concern(m.value, ms.anchors))
    return concerns

RED_FLAG_CONCERN = 70.0
GREEN_FLAG_CONCERN = 20.0

_FLAG_PHRASES: dict[str, tuple[str, str]] = {
    # metric -> (red flag title, green flag title)
    "receivables_growth_spread": (
        "Receivables outpacing revenue",
        "Receivables growing in line with revenue",
    ),
    "cfo_to_net_income": (
        "Operating cash flow lagging reported earnings",
        "Operating cash flow fully supports reported earnings",
    ),
    "total_accruals": ("Elevated accrual intensity", "Low accrual intensity"),
    "beneish_m_score": (
        "Beneish screen in the elevated-attention zone",
        "Beneish screen in the low-attention zone",
    ),
    "sbc_to_revenue": ("Heavy stock-based compensation burden", "Modest stock-based compensation"),
    "diluted_share_growth": ("Accelerating dilution", "Stable or shrinking share count"),
    "capex_growth_spread": ("Capex growing well ahead of revenue", "Capex disciplined relative to revenue"),
    "inventory_growth_spread": ("Inventory building ahead of demand", "Inventory in line with revenue"),
    "net_debt_to_ebitda": ("Elevated leverage", "Conservative leverage"),
    "interest_coverage": ("Thin interest coverage", "Comfortable interest coverage"),
    "fcf_margin": ("Weak free-cash-flow generation", "Strong free-cash-flow generation"),
    "adjustment_recurrence_ratio": (
        "Recurring 'non-recurring' adjustment language",
        "Little adjustment-heavy language",
    ),
    "kpi_removals": ("Previously highlighted KPIs no longer disclosed", "Consistent KPI disclosure"),
}


def _flag_detail(m: MetricResult, concern: float, severity: str) -> str:
    closing = "Requires analyst review." if severity == "red" else "Supportive indicator."
    return (
        f"{m.name} = {m.value:.3g} (formula: {m.formula}; period {m.fiscal_label}; "
        f"concern {concern:.0f}/100). {closing}"
    )


def _generate_flags(block_components, metrics_by_name: dict[str, MetricResult]) -> tuple[list[Flag], list[Flag]]:
    red: list[tuple[float, Flag]] = []
    green: list[tuple[float, Flag]] = []
    seen: set[str] = set()
    for block in block_components:
        for comp in block.components:
            if comp.concern_score is None or comp.metric_name in seen:
                continue
            # P0-13: a zero-weighted component must never generate a flag —
            # exclusion from the score means exclusion from the flag list.
            if comp.weight == 0:
                continue
            m = metrics_by_name.get(comp.metric_name)
            if m is None or m.value is None:
                continue
            seen.add(comp.metric_name)
            titles = _FLAG_PHRASES.get(comp.metric_name)
            if comp.concern_score >= RED_FLAG_CONCERN:
                title = titles[0] if titles else f"Elevated concern: {comp.metric_name}"
                red.append(
                    (
                        comp.concern_score,
                        Flag(
                            severity="red",
                            title=title,
                            detail=_flag_detail(m, comp.concern_score, "red"),
                            evidence_metrics=[m.name],
                            fiscal_label=m.fiscal_label,
                        ),
                    )
                )
            elif comp.concern_score <= GREEN_FLAG_CONCERN:
                title = titles[1] if titles else f"Supportive: {comp.metric_name}"
                green.append(
                    (
                        comp.concern_score,
                        Flag(
                            severity="green",
                            title=title,
                            detail=_flag_detail(m, comp.concern_score, "green"),
                            evidence_metrics=[m.name],
                            fiscal_label=m.fiscal_label,
                        ),
                    )
                )
    # Rank by severity: highest concern first for red, lowest first for green.
    red.sort(key=lambda cf: (-cf[0], cf[1].title))
    green.sort(key=lambda cf: (cf[0], cf[1].title))
    return [f for _, f in red[:10]], [f for _, f in green[:10]]


_CHANGE_METRICS = [
    ("total_accruals", "Total accruals", "{:.3f}"),
    ("cfo_to_net_income", "CFO / Net income", "{:.2f}"),
    ("receivables_growth_spread", "Receivables-vs-revenue growth spread", "{:+.1%}"),
    ("dso", "Days sales outstanding", "{:.0f}"),
    ("sbc_to_revenue", "SBC / Revenue", "{:.1%}"),
    ("capex_to_revenue", "Capex / Revenue", "{:.1%}"),
    ("fcf_margin", "FCF margin", "{:.1%}"),
]


def _what_changed(bundle: MetricsBundle) -> list[str]:
    changes: list[str] = []
    for name, label, fmt in _CHANGE_METRICS:
        series = [m for m in bundle.history.get(name, []) if m.status is MetricStatus.OK]
        if len(series) < 2:
            continue
        cur, prev = series[-1], series[-2]
        changes.append(
            f"{label}: {fmt.format(prev.value)} ({prev.fiscal_label}) -> "
            f"{fmt.format(cur.value)} ({cur.fiscal_label})"
        )
    return changes


def _evidence_ledger(metrics: list[MetricResult]) -> list[EvidenceEntry]:
    entries: list[EvidenceEntry] = []
    for m in metrics:
        if m.status is not MetricStatus.OK or m.value is None:
            continue
        entries.append(
            EvidenceEntry(
                claim=f"{m.name} computed as {m.value:.4g} for {m.fiscal_label}",
                metric_name=m.name,
                formula=m.formula,
                inputs=m.inputs,
                value=m.value,
                fiscal_label=m.fiscal_label,
            )
        )
    return entries


_MISMATCH_QUESTIONS = {
    "demand_narrative_vs_working_capital": (
        "Management describes demand as strong; what explains the concurrent "
        "receivables/inventory build relative to revenue?"
    ),
    "profitability_narrative_vs_cash_conversion": (
        "Reported profitability is emphasized; why is operating/free cash flow "
        "not converting at a comparable rate, and when should it?"
    ),
    "buyback_narrative_vs_share_count": (
        "Repurchases are highlighted while net share count rose; how much of the "
        "program offsets compensation issuance versus reducing the float?"
    ),
    "growth_capex_narrative_vs_fcf": (
        "Capex is framed as growth investment; what unit-level returns support it "
        "given the compression in free-cash-flow margin?"
    ),
}


def _analyst_questions(red_flags: list[Flag], narrative_findings, mismatches=()) -> list[str]:
    questions: list[str] = []
    for mm in mismatches:
        q = _MISMATCH_QUESTIONS.get(mm.kind)
        if q and q not in questions:
            questions.append(q)
    templates = {
        "Receivables outpacing revenue": (
            "What drove receivables growth ahead of revenue this period — payment-term "
            "changes, channel mix, or collection timing?"
        ),
        "Operating cash flow lagging reported earnings": (
            "Which accrual items explain the gap between net income and operating cash flow, "
            "and are they expected to reverse?"
        ),
        "Heavy stock-based compensation burden": (
            "How does management justify current SBC levels relative to revenue, and what is "
            "the expected dilution trajectory?"
        ),
        "Capex growing well ahead of revenue": (
            "What unit-level evidence supports the return profile of the accelerated capex program?"
        ),
        "Recurring 'non-recurring' adjustment language": (
            "Why have adjustment categories described as one-time recurred across multiple "
            "periods, and what would make them stop?"
        ),
        "Previously highlighted KPIs no longer disclosed": (
            "Why were previously disclosed KPIs dropped from this period's materials?"
        ),
    }
    for flag in red_flags:
        q = templates.get(flag.title)
        if q and q not in questions:
            questions.append(q)
    for finding in narrative_findings:
        if finding.kind == "kpi_removed" and len(questions) < 10:
            questions.append(f"Regarding {finding.fiscal_label}: {finding.detail}")
    if not questions:
        questions.append(
            "No elevated-concern items were flagged; confirm data completeness before "
            "concluding the period is clean."
        )
    return questions[:10]


def analyze(dataset: CompanyDataset) -> AnalysisResult:
    profile = dataset.profile
    periods = dataset.sorted_periods()
    labels = [p.fiscal_label for p in periods]

    if profile.is_financial_institution:
        return AnalysisResult(
            profile=profile,
            analyzed_periods=labels,
            excluded=True,
            exclusion_reason=(
                "Financial institutions (banks, insurers) are excluded: accrual-, "
                "working-capital-, and leverage-based formulas in this engine are not "
                "meaningful for financial-institution balance sheets. A dedicated "
                "financial-sector module is on the roadmap."
            ),
        )

    bundle = compute_metrics(dataset)
    financial_by_name = {m.name: m for m in bundle.latest}
    narrative = compute_narrative_layer(
        dataset.documents,
        metrics_by_name=financial_by_name,
        concern_by_name=_financial_concerns(financial_by_name),
    )
    narrative_metrics, narrative_findings = narrative.metrics, narrative.findings
    all_metrics = bundle.latest + narrative_metrics

    sgi = bundle.get_latest("beneish_sgi")
    high_growth = (
        sgi is not None
        and sgi.status is MetricStatus.OK
        and sgi.value is not None
        and sgi.value > cfg.HIGH_GROWTH_SGI_THRESHOLD
    )

    block_scores, overall = score_all(all_metrics, sector=profile.sector, high_growth=high_growth)
    metrics_by_name = {m.name: m for m in all_metrics}
    red, green = _generate_flags(block_scores, metrics_by_name)

    return AnalysisResult(
        profile=profile,
        analyzed_periods=labels,
        metrics=all_metrics,
        block_scores=block_scores,
        overall=overall,
        red_flags=red,
        green_flags=green,
        changes=_what_changed(bundle),
        evidence=_evidence_ledger(all_metrics),
        narrative_findings=narrative_findings,
        narrative_evidence=narrative.evidence,
        mismatches=narrative.mismatches,
        analyst_questions=_analyst_questions(red, narrative_findings, narrative.mismatches),
    )
