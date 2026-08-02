"""Narrative layer orchestrator (v0.4).

Runs every deterministic narrative detector over the period documents,
assembles the narrative evidence ledger, computes scoring-layer metrics, and
runs metric-narrative mismatch checks against the deterministic financial
metrics. All under the same contract as financial formulas: missing inputs
are reported, never silently dropped, and every claim carries evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.financials import DocumentRecord
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import MetricNarrativeMismatch, NarrativeEvidence, NarrativeFinding
from app.services.narrative import adjustment_language, kpi_drift
from app.services.narrative.baselines import build_comparison, group_documents
from app.services.narrative.detectors import (
    detect_defensive_tone,
    detect_guidance_shift,
    detect_risk_factor_changes,
)
from app.services.narrative.evidence import EvidenceLedger
from app.services.narrative.mismatch import detect_mismatches

_MISSING_SPECS = {
    "adjustment_recurrence_ratio": ("periods with adjustment language / periods analyzed", "documents for >= 3 periods"),
    "recurring_adjustment_terms": ("count of terms appearing in >= 3 periods", "documents for >= 3 periods"),
    "kpi_removals": ("KPIs in prior periods absent from latest", "documents for >= 2 periods"),
    "disclosure_volume_change": ("latest period word count / mean(prior period word counts)", "documents for >= 3 periods"),
    "defensive_tone_change": ("defensive-term density per 1k words vs trailing baseline", "documents for >= 2 periods"),
    "guidance_shift": ("prior guidance stance - current guidance stance", "guidance discussion in >= 2 periods"),
    "risk_factor_expansion": ("current risk-factor words / prior risk-factor words", "risk-factor sections for >= 2 comparable periods"),
}


def _missing(name: str, reason: str | None = None) -> MetricResult:
    formula, default_reason = _MISSING_SPECS[name]
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label="n/a",
        status=MetricStatus.MISSING_DATA,
        missing_fields=[reason or default_reason],
    )


def _ok(name: str, label: str, value: float, inputs: dict[str, float] | None = None, note: str | None = None) -> MetricResult:
    return MetricResult(
        name=name,
        formula=_MISSING_SPECS[name][0],
        fiscal_label=label,
        status=MetricStatus.OK,
        value=value,
        inputs=inputs or {},
        note=note,
    )


@dataclass
class NarrativeLayerResult:
    metrics: list[MetricResult] = field(default_factory=list)
    findings: list[NarrativeFinding] = field(default_factory=list)
    evidence: list[NarrativeEvidence] = field(default_factory=list)
    mismatches: list[MetricNarrativeMismatch] = field(default_factory=list)


def compute_narrative_layer(
    documents: list[DocumentRecord],
    metrics_by_name: dict[str, MetricResult] | None = None,
    concern_by_name: dict[str, float] | None = None,
) -> NarrativeLayerResult:
    result = NarrativeLayerResult()
    if not documents:
        result.metrics = [_missing(name, "no documents provided") for name in _MISSING_SPECS]
        return result

    ledger = EvidenceLedger()
    periods = group_documents(documents)
    cmp = build_comparison(periods)
    label = periods[-1].fiscal_label
    n_periods = len(periods)

    # --- adjustment language (existing detector) --------------------------
    adj = adjustment_language.analyze(documents)
    if adj.periods_analyzed >= 3:
        result.metrics.append(
            _ok("adjustment_recurrence_ratio", label, adj.recurrence_ratio,  # type: ignore[arg-type]
                {"periods_analyzed": float(adj.periods_analyzed)})
        )
        result.metrics.append(
            _ok("recurring_adjustment_terms", label, float(len(adj.recurring_terms)),
                {t: float(c) for t, c in adj.recurring_terms.items()})
        )
    else:
        result.metrics.append(_missing("adjustment_recurrence_ratio"))
        result.metrics.append(_missing("recurring_adjustment_terms"))
    result.findings.extend(adj.findings)
    for f in adj.findings:
        ledger.add(
            detector="adjustment_recurrence",
            fiscal_label=f.fiscal_label,
            comparison="trailing8",
            source="period documents",
            excerpt=" | ".join(f.evidence_snippets[:2]) or f.detail,
            detail=f.detail,
            confidence="high" if adj.periods_analyzed >= 6 else "medium",
        )

    # --- KPI additions/removals + definition changes -----------------------
    kpi = kpi_drift.analyze(documents)
    if kpi.periods_analyzed >= 2:
        result.metrics.append(
            _ok("kpi_removals", label, float(len(kpi.removed)),
                note=("Removed: " + ", ".join(sorted(kpi.removed))) if kpi.removed else None)
        )
    else:
        result.metrics.append(_missing("kpi_removals"))
    result.findings.extend(kpi.findings)
    for f in kpi.findings:
        ledger.add(
            detector=f.kind,
            fiscal_label=f.fiscal_label,
            comparison="qoq",
            source="period documents",
            excerpt=f.detail,
            detail=f.detail,
            confidence="medium",
        )

    # P0-C (roadmap): kpi_definition_change UNWIRED from the live layer. The
    # Phase-4 validation + isolation spike concluded the single-sentence
    # extractor's hits are mostly extraction artifacts (~1/10 genuine); the
    # signal is shelved (docs/kpi_definition_isolation_spike.md). The hardened
    # extractor/adjudicator (kpi_extraction.py / kpi_adjudicator.py) remains
    # available to validation scripts but does not feed reports.

    # --- disclosure volume --------------------------------------------------
    if n_periods >= 3:
        latest_words = periods[-1].word_count()
        prior_counts = [p.word_count() for p in periods[:-1]]
        prior_mean = sum(prior_counts) / len(prior_counts)
        if prior_mean > 0:
            ratio = latest_words / prior_mean
            result.metrics.append(
                _ok("disclosure_volume_change", label, ratio,
                    {"latest_words": float(latest_words), "prior_mean_words": prior_mean})
            )
            if ratio < 0.75:
                detail = (
                    f"Disclosure volume fell to {ratio:.0%} of the trailing average "
                    f"({latest_words} words vs {prior_mean:.0f}). Reduced disclosure requires review."
                )
                result.findings.append(
                    NarrativeFinding(kind="disclosure_reduction", detail=detail, fiscal_label=label)
                )
                ledger.add(
                    detector="disclosure_reduction", fiscal_label=label, comparison="trailing8",
                    source="period documents", excerpt=detail, detail=detail, confidence="medium",
                )
        else:
            result.metrics.append(_missing("disclosure_volume_change", "non-empty prior documents"))
    else:
        result.metrics.append(_missing("disclosure_volume_change"))

    # --- defensive tone ------------------------------------------------------
    tone = detect_defensive_tone(cmp) if cmp else None
    if tone and tone.change is not None:
        result.metrics.append(
            _ok("defensive_tone_change", label, tone.change,
                {"current_density": tone.current_density, "baseline_density": tone.baseline_density})  # type: ignore[dict-item]
        )
        if tone.change > 1.0 and tone.excerpts:
            detail = (
                f"Defensive/hedging language rose to {tone.current_density:.1f} per 1k words "
                f"vs a trailing baseline of {tone.baseline_density:.1f}."
            )
            result.findings.append(
                NarrativeFinding(kind="defensive_tone_increase", detail=detail,
                                 fiscal_label=label, evidence_snippets=tone.excerpts[:2])
            )
            ledger.add(
                detector="defensive_tone_increase", fiscal_label=label, comparison="trailing8",
                source="period documents", excerpt=tone.excerpts[0], detail=detail, confidence="medium",
            )
    else:
        result.metrics.append(_missing("defensive_tone_change"))

    # --- guidance shift -------------------------------------------------------
    guid = detect_guidance_shift(cmp) if cmp else None
    if guid and guid.shift is not None and guid.guidance_discussed:
        result.metrics.append(
            _ok("guidance_shift", label, guid.shift,
                {"current_stance": float(guid.current_stance), "prior_stance": float(guid.prior_stance)})  # type: ignore[arg-type]
        )
        if guid.shift >= 2 and guid.excerpts:
            detail = (
                f"Guidance stance weakened vs prior period (stance {guid.prior_stance} -> "
                f"{guid.current_stance}; positive-minus-negative guidance-term counts)."
            )
            result.findings.append(
                NarrativeFinding(kind="guidance_shift", detail=detail,
                                 fiscal_label=label, evidence_snippets=guid.excerpts[:2])
            )
            ledger.add(
                detector="guidance_shift", fiscal_label=label, comparison="qoq",
                source="period documents", excerpt=guid.excerpts[0], detail=detail, confidence="medium",
            )
    else:
        result.metrics.append(_missing("guidance_shift"))

    # --- risk factors ----------------------------------------------------------
    risk = detect_risk_factor_changes(cmp) if cmp else None
    if risk and risk.expansion_ratio is not None:
        result.metrics.append(
            _ok("risk_factor_expansion", label, risk.expansion_ratio,
                note=f"comparison basis: {risk.comparison_basis}")
        )
    else:
        result.metrics.append(_missing("risk_factor_expansion"))
    if risk and risk.new_high_severity_terms:
        for term, excerpt in zip(risk.new_high_severity_terms, risk.excerpts):
            detail = (
                f'High-severity disclosure term "{term}" appears for the first time in the '
                "trailing window. Requires immediate analyst review."
            )
            result.findings.append(
                NarrativeFinding(kind="high_severity_disclosure", detail=detail,
                                 fiscal_label=label, evidence_snippets=[excerpt])
            )
            ledger.add(
                detector="high_severity_disclosure", fiscal_label=label, comparison="trailing8",
                source="period documents", excerpt=excerpt, detail=detail, confidence="high",
            )

    # --- metric-narrative mismatches --------------------------------------------
    if cmp and metrics_by_name and concern_by_name:
        result.mismatches = detect_mismatches(cmp, metrics_by_name, concern_by_name, ledger)

    result.evidence = ledger.entries
    return result


def compute_narrative_metrics(
    documents: list[DocumentRecord],
) -> tuple[list[MetricResult], list[NarrativeFinding]]:
    """Backward-compatible wrapper (v0.1 signature)."""
    layer = compute_narrative_layer(documents)
    return layer.metrics, layer.findings
