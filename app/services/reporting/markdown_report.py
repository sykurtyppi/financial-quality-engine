"""Markdown report generator.

Renders an AnalysisResult into an analyst-grade report. Deterministic:
identical inputs (including `generated_on`) produce byte-identical output,
which is what the golden-report tests assert.
"""

from __future__ import annotations

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

_DIRECTION_LABEL = {
    Direction.POSITIVE: "Positive",
    Direction.MIXED: "Mixed",
    Direction.NEGATIVE: "Negative",
}


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}g}"


def _overall_assessment(result: AnalysisResult) -> str:
    if result.overall is None or result.overall.score is None:
        return "insufficient data for an overall assessment"
    s = result.overall.score
    if s < 30:
        return "no elevated earnings-quality concerns identified by the screen"
    if s <= 50:
        return "mixed profile; selected items warrant analyst review"
    if s <= 70:
        return "elevated earnings-quality risk indicators; analyst review recommended"
    return "multiple elevated risk indicators; thorough analyst review recommended"


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
    if overall and overall.score is not None:
        add(
            f"Overall Quality Risk Score: **{overall.score:.0f}/100** "
            f"({_DIRECTION_LABEL[overall.direction]}, confidence {overall.confidence.value}). "
            f"Assessment: {_overall_assessment(result)}."
        )
        add("")
        add(overall.rationale)
    else:
        add(f"Assessment: {_overall_assessment(result)}.")
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
        block_caveats = [c for c in bs.caveats if "v0 heuristic" not in c]
        for caveat in block_caveats:
            add(f"> {bs.name}: {caveat}")
    add("")

    # 3. Red flags
    add("## 3. Top Red Flags")
    add("")
    if result.red_flags:
        for f in result.red_flags:
            add(f"- **{f.title}** ({f.fiscal_label}): {f.detail}")
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

    # Narrative findings
    if result.narrative_findings:
        add("### Narrative & Disclosure Observations")
        add("")
        for nf in result.narrative_findings:
            add(f"- *{nf.kind}* ({nf.fiscal_label}): {nf.detail}")
            for snip in nf.evidence_snippets[:2]:
                add(f"  - Evidence: {snip}")
        add("")

    # 6. Evidence ledger
    add("## 6. Evidence Ledger")
    add("")
    add("| Metric | Period | Value | Formula | Inputs |")
    add("|---|---|---|---|---|")
    for e in result.evidence:
        inputs = ", ".join(f"{k}={_fmt(v)}" for k, v in e.inputs.items())
        add(f"| {e.metric_name} | {e.fiscal_label} | {_fmt(e.value, 4)} | {e.formula} | {inputs} |")
    add("")

    # 7. Metric detail incl. missing data
    add("## 7. Metric Detail (including data gaps)")
    add("")
    add("| Metric | Period | Status | Value | Note |")
    add("|---|---|---|---|---|")
    for m in result.metrics:
        note = m.note or (
            "Missing: " + ", ".join(m.missing_fields) if m.missing_fields else ""
        )
        add(
            f"| {m.name} | {m.fiscal_label} | {m.status.value} | "
            f"{_fmt(m.value, 4)} | {note} |"
        )
    add("")
    n_missing = sum(1 for m in result.metrics if m.status is MetricStatus.MISSING_DATA)
    if n_missing:
        add(
            f"*{n_missing} metric(s) could not be computed due to missing input data "
            "and are reported above rather than silently dropped.*"
        )
        add("")

    # 8. Analyst review questions
    add("## 8. Analyst Review Questions")
    add("")
    for q in result.analyst_questions:
        add(f"- {q}")
    add("")

    # 9. Disclaimer
    add("## 9. Disclaimer")
    add("")
    add(DISCLAIMER)
    return "\n".join(lines) + "\n"
