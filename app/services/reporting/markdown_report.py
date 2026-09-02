"""Markdown report generator.

Renders an AnalysisResult into an analyst-grade report. Deterministic:
identical inputs (including `generated_on`) produce byte-identical output,
which is what the golden-report tests assert.
"""

from __future__ import annotations

from app.config import scoring_config
from app.schemas.metrics import MetricStatus
from app.schemas.report import AnalysisResult
from app.schemas.scoring import Direction

DISCLAIMER = (
    "This report is an automated, formula-driven screening analysis of publicly "
    "reported financial data. It expresses opinions about earnings quality and "
    "presentation risk derived from the disclosed formulas herein. It does not "
    "allege fraud, misconduct, or wrongdoing by any company or person, and it is "
    "not investment advice. Elevated scores identify areas that warrant analyst "
    "review; they are not conclusions. Data may contain errors; formulas use v0 "
    "heuristic thresholds that are not backtested or sector-normalized."
)

# AMKR-class misread (2026Q2): the engine's cash-conversion flags do not model
# grants, tax credits, customer advances, or milestone receipts. Those items
# may affect reported operating cash flow, investing cash flow, or net capital
# spending depending on their terms and presentation — the engine cannot tell,
# so the note stays NEUTRAL about where they land and is split by metric: a
# CFO-based flag gets no capex-centered explanation attached to it.
FUNDING_CONTEXT_METRICS_CFO = {"cfo_to_net_income"}
FUNDING_CONTEXT_METRICS_FCF = {"fcf_margin", "fcf_margin_trend"}
FUNDING_CONTEXT_METRICS = FUNDING_CONTEXT_METRICS_CFO | FUNDING_CONTEXT_METRICS_FCF
FUNDING_CONTEXT_NOTE_CFO = (
    "> Funding context (check before treating weak cash conversion as "
    "deterioration): government grants, tax credits, customer advances, and "
    "milestone receipts may affect reported operating cash flow depending on "
    "their terms and presentation. Their classification is not separately "
    "modeled here — verify in the filing's liquidity/commitments notes "
    "(measured misread: AMKR 2026Q2)."
)
FUNDING_CONTEXT_NOTE_FCF = (
    "> Funding context (check before treating a weak free-cash-flow reading as "
    "deterioration): government grants, tax credits, customer advances, and "
    "milestone receipts may affect reported operating cash flow, investing "
    "cash flow, or net capital spending depending on their terms and "
    "presentation. Their classification is not separately modeled here — "
    "verify in the filing's liquidity/commitments notes (measured misread: "
    "AMKR 2026Q2)."
)

_DIRECTION_LABEL = {
    Direction.POSITIVE: "Positive",
    Direction.MIXED: "Mixed",
    Direction.NEGATIVE: "Negative",
}


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}g}"


def render(result: AnalysisResult, generated_on: str) -> str:
    p = result.profile
    lines: list[str] = []
    add = lines.append

    add(f"# Earnings Quality & Narrative Drift Report — {p.ticker}")
    add("")
    add(f"*Generated {generated_on} · Periods analyzed: {', '.join(result.analyzed_periods)}*")
    add("")

    if result.excluded:
        add("## Coverage Exclusion")
        add("")
        add(result.exclusion_reason or "Excluded.")
        add("")
        add("## Disclaimer")
        add("")
        add(DISCLAIMER)
        return "\n".join(lines) + "\n"

    # 1. Executive summary
    add("## 1. Executive Summary")
    add("")
    overall = result.overall
    add(
        "No single composite grade is asserted — the 0–100 composite measured "
        "non-discriminating on the live season and is retired from the headline. "
        "This report is evidence detail, dimension by dimension. An EXPERIMENTAL "
        "distress reading (not yet validated) appears on the decision card; it is "
        "not a replacement aggregate."
    )
    add("")
    add(
        f"The screen flagged {len(result.red_flags)} elevated-concern item(s) and "
        f"{len(result.green_flags)} supportive item(s) for {p.ticker} in "
        f"{result.analyzed_periods[-1]}. Key changes versus prior periods are listed in §5."
    )
    if overall:
        for caveat in overall.caveats:
            add("")
            add(f"> Caveat: {caveat}")
    add("")

    # 2. Scorecard
    add("## 2. Scorecard")
    add("")
    add("All scores are 0–100 concern scores: 0 = no concern, 100 = maximum concern.")
    add("")
    add("| Block | Score | Direction | Confidence | Coverage | Weight |")
    add("|---|---|---|---|---|---|")
    for bs in result.block_scores:
        weight = overall.block_weights.get(bs.name, 0.0) if overall else 0.0
        score_txt = f"{bs.score:.0f}" if bs.score is not None else "n/a"
        add(
            f"| {bs.name} | {score_txt} | {_DIRECTION_LABEL[bs.direction]} | "
            f"{bs.confidence.value} | {bs.data_coverage:.0%} | {weight:.0%} |"
        )
    add("")
    for bs in result.block_scores:
        block_caveats = [c for c in bs.caveats if c != scoring_config.V0_WEIGHTS_CAVEAT]
        for caveat in block_caveats:
            add(f"> {bs.name}: {caveat}")
    add("")

    # 3. Red flags
    add("## 3. Top Red Flags")
    add("")
    if result.red_flags:
        for f in result.red_flags:
            add(f"- **{f.title}** ({f.fiscal_label}): {f.detail}")
        flagged = set().union(*(set(f.evidence_metrics) for f in result.red_flags))
        if flagged & FUNDING_CONTEXT_METRICS_FCF:
            add("")
            add(FUNDING_CONTEXT_NOTE_FCF)
        elif flagged & FUNDING_CONTEXT_METRICS_CFO:
            add("")
            add(FUNDING_CONTEXT_NOTE_CFO)
    else:
        add("- No metrics crossed the elevated-concern threshold this period.")
    add("")

    # 4. Green flags
    add("## 4. Top Green Flags")
    add("")
    if result.green_flags:
        for f in result.green_flags:
            add(f"- **{f.title}** ({f.fiscal_label}): {f.detail}")
    else:
        add("- No metrics reached the supportive threshold this period.")
    add("")

    # 5. What changed
    add("## 5. What Changed This Period")
    add("")
    if result.changes:
        for c in result.changes:
            add(f"- {c}")
    else:
        add("- Insufficient period history to report changes.")
    add("")

    # 6. Narrative drift summary
    add("## 6. Narrative Drift Summary")
    add("")
    if result.narrative_findings:
        add(
            "Deterministic language analysis across the documented periods "
            "(QoQ, YoY, and trailing-8-quarter baselines where available). "
            "Findings are review prompts; each cites its evidence in §8."
        )
        add("")
        for nf in result.narrative_findings:
            add(f"- *{nf.kind}* ({nf.fiscal_label}): {nf.detail}")
            for snip in nf.evidence_snippets[:2]:
                add(f"  - Evidence: {snip}")
    else:
        add(
            "No narrative drift findings — either the language profile is stable "
            "or insufficient documents were provided (see §9 data gaps)."
        )
    add("")

    # 7. Metric/narrative mismatches
    add("## 7. Metric/Narrative Mismatches")
    add("")
    if result.mismatches:
        add(
            "Places where management narrative and deterministic metrics point in "
            "different directions. A mismatch is a question to resolve, not a "
            "conclusion — the narrative may be fully justified."
        )
        add("")
        for mm in result.mismatches:
            values = ", ".join(f"{k}={_fmt(v, 3)}" for k, v in mm.metric_values.items())
            add(f"- **{mm.kind}** ({mm.fiscal_label}, confidence {mm.confidence}): {mm.detail}")
            add(f"  - Metrics: {values or ', '.join(mm.metric_names)}")
            add(f"  - Narrative evidence: {mm.narrative_evidence_id} (§8b)")
    else:
        add("- No metric/narrative mismatches detected this period.")
    add("")

    # 8. Evidence ledger
    add("## 8. Evidence Ledger")
    add("")
    add("### 8a. Metric evidence")
    add("")
    add("| Metric | Period | Value | Formula | Inputs |")
    add("|---|---|---|---|---|")
    for e in result.evidence:
        inputs = ", ".join(f"{k}={_fmt(v)}" for k, v in e.inputs.items())
        add(f"| {e.metric_name} | {e.fiscal_label} | {_fmt(e.value, 4)} | {e.formula} | {inputs} |")
    add("")
    if result.narrative_evidence:
        add("### 8b. Narrative evidence")
        add("")
        add("| ID | Detector | Period | Basis | Confidence | Linked metrics | Excerpt |")
        add("|---|---|---|---|---|---|---|")
        for ne in result.narrative_evidence:
            excerpt = ne.excerpt.replace("|", "\\|")
            add(
                f"| {ne.evidence_id} | {ne.detector} | {ne.fiscal_label} | {ne.comparison} | "
                f"{ne.confidence} | {', '.join(ne.linked_metrics) or '—'} | {excerpt} |"
            )
        add("")

    # 9. Metric detail incl. data gaps, grouped by status
    add("## 9. Metric Detail and Data Quality")
    add("")
    add("How to read this section — four distinct situations, never conflated:")
    add("")
    add(
        "- **Computed** — the metric was calculated; whether it is a concern is "
        "judged in the scorecard (§2) and red flags (§3), not by its mere presence here."
    )
    add(
        "- **Data unavailable** — the filer does not disclose an input (or our "
        "mapping could not locate it). This is a coverage gap, NOT evidence of a problem."
    )
    add(
        "- **Not meaningful** — inputs exist but the ratio is undefined for this "
        "company's situation (e.g. earnings-based ratios during a loss period). "
        "The note says why; review the underlying levels directly."
    )
    add(
        "- **Sector/model caveats** — quoted lines under the scorecard (§2); they "
        "qualify interpretation (e.g. high-growth profile) without changing computed values."
    )
    add("")
    ok_metrics = [m for m in result.metrics if m.status is MetricStatus.OK]
    nm_metrics = [m for m in result.metrics if m.status is MetricStatus.NOT_MEANINGFUL]
    missing_metrics = [m for m in result.metrics if m.status is MetricStatus.MISSING_DATA]
    add(
        f"Coverage: **{len(ok_metrics)} computed**, {len(nm_metrics)} not meaningful, "
        f"{len(missing_metrics)} with data unavailable (out of {len(result.metrics)} metrics)."
    )
    add("")
    add("### Computed metrics")
    add("")
    add("| Metric | Period | Value | Note |")
    add("|---|---|---|---|")
    for m in ok_metrics:
        add(f"| {m.name} | {m.fiscal_label} | {_fmt(m.value, 4)} | {m.note or ''} |")
    add("")
    if nm_metrics:
        add("### Not meaningful for this company/period")
        add("")
        add("| Metric | Period | Why |")
        add("|---|---|---|")
        for m in nm_metrics:
            add(f"| {m.name} | {m.fiscal_label} | {m.note or 'undefined for these inputs'} |")
        add("")
    if missing_metrics:
        add("### Data unavailable (reported, not silently dropped)")
        add("")
        add("| Metric | Period | Missing inputs |")
        add("|---|---|---|")
        for m in missing_metrics:
            add(f"| {m.name} | {m.fiscal_label} | {', '.join(m.missing_fields) or '—'} |")
        add("")

    # 10. Analyst review questions
    add("## 10. Analyst Review Questions")
    add("")
    for q in result.analyst_questions:
        add(f"- {q}")
    add("")

    # 11. Disclaimer
    add("## 11. Disclaimer")
    add("")
    add(DISCLAIMER)
    return "\n".join(lines) + "\n"
