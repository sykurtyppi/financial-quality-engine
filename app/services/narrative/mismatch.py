"""Metric-narrative mismatch checks.

Each check pairs a positive management-narrative signal (lexicon match with a
verbatim excerpt) against deterministic metrics pointing the other way. A
mismatch is a REVIEW PROMPT — the narrative may be entirely justified; the
point is that the claim and the numbers deserve to be read together.

Confidence: "high" when the metric side crosses the red-flag line (concern >=
70) AND the narrative emphasis appears more than once; otherwise "medium".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import MetricNarrativeMismatch
from app.services.narrative.baselines import ComparisonSet
from app.services.narrative.evidence import EvidenceLedger

CONCERN_TRIGGER = 60.0
HIGH_CONFIDENCE_CONCERN = 70.0
# Share-count rise below this (per quarter) is vesting-timing noise, not a
# buyback-narrative contradiction (live finding: AAPL +0.04% single-quarter
# uptick while shrinking the float ~3%/year).
SHARE_COUNT_RISE_MATERIALITY = 0.005


@dataclass(frozen=True)
class MismatchSpec:
    kind: str
    narrative_terms: tuple[str, ...]
    metric_names: tuple[str, ...]
    detail_template: str


MISMATCH_SPECS: tuple[MismatchSpec, ...] = (
    MismatchSpec(
        kind="demand_narrative_vs_working_capital",
        narrative_terms=(
            "strong demand", "robust demand", "demand remains strong", "demand remained strong",
            "record bookings", "strong momentum", "demand is strong", "healthy demand",
        ),
        metric_names=("receivables_growth_spread", "inventory_growth_spread", "dso_trend"),
        detail_template=(
            "Management emphasizes demand strength while {metrics} indicate "
            "working-capital deterioration. The demand narrative and the "
            "receivables/inventory build require joint review."
        ),
    ),
    MismatchSpec(
        kind="profitability_narrative_vs_cash_conversion",
        narrative_terms=(
            "record profitability", "margin expansion", "improved profitability",
            "strong profitability", "profitability improved", "record margins",
            "adjusted ebitda margin expansion",
        ),
        metric_names=("cfo_to_net_income", "fcf_margin_trend", "fcf_to_net_income"),
        detail_template=(
            "Management emphasizes profitability while {metrics} show cash "
            "conversion weakening. Whether reported profitability is converting "
            "to cash requires review."
        ),
    ),
    MismatchSpec(
        kind="buyback_narrative_vs_share_count",
        narrative_terms=(
            "share repurchase", "buyback", "returning capital to shareholders",
            "returned to shareholders", "repurchase program",
        ),
        metric_names=("net_share_count_change",),
        detail_template=(
            "Management highlights repurchases while the share count still rose "
            "({metrics}). Repurchases may primarily offset compensation "
            "issuance; net effect requires review."
        ),
    ),
    MismatchSpec(
        kind="growth_capex_narrative_vs_fcf",
        narrative_terms=(
            "investing for growth", "growth investments", "capacity expansion",
            "strategic investments", "invest ahead of demand", "investing in ai",
        ),
        metric_names=("capex_growth_spread", "fcf_margin_trend", "capex_intensity_regime_shift"),
        detail_template=(
            "Capex is framed as growth investment while {metrics} show free-cash-"
            "flow compression or a capex regime shift. The return profile of the "
            "investment program requires review."
        ),
    ),
)


def _find_narrative(text: str, terms: tuple[str, ...]) -> tuple[int, str | None]:
    """(match count, first excerpt)."""
    count = 0
    excerpt: str | None = None
    for term in terms:
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
            count += 1
            if excerpt is None:
                start = max(0, m.start() - 90)
                end = min(len(text), m.end() + 90)
                excerpt = "…" + " ".join(text[start:end].split()) + "…"
    return count, excerpt


def detect_mismatches(
    cmp: ComparisonSet,
    metrics_by_name: dict[str, MetricResult],
    concern_by_name: dict[str, float],
    ledger: EvidenceLedger,
) -> list[MetricNarrativeMismatch]:
    text = cmp.current.all_text
    label = cmp.current.fiscal_label
    out: list[MetricNarrativeMismatch] = []

    for spec in MISMATCH_SPECS:
        mentions, excerpt = _find_narrative(text, spec.narrative_terms)
        if mentions == 0 or excerpt is None:
            continue

        if spec.kind == "buyback_narrative_vs_share_count":
            # Metric trigger is the raw value (share count still rising), not
            # its concern mapping.
            m = metrics_by_name.get("net_share_count_change")
            if (
                m is None
                or m.status is not MetricStatus.OK
                or m.value is None
                or m.value <= SHARE_COUNT_RISE_MATERIALITY
            ):
                continue
            triggered = ["net_share_count_change"]
            max_concern = concern_by_name.get("net_share_count_change", 0.0)
        else:
            triggered = [
                name
                for name in spec.metric_names
                if concern_by_name.get(name, 0.0) >= CONCERN_TRIGGER
            ]
            if not triggered:
                continue
            max_concern = max(concern_by_name[n] for n in triggered)

        confidence = "high" if (max_concern >= HIGH_CONFIDENCE_CONCERN and mentions > 1) else "medium"
        detail = spec.detail_template.format(metrics=", ".join(triggered))
        entry = ledger.add(
            detector=f"mismatch:{spec.kind}",
            fiscal_label=label,
            comparison="point",
            source="period documents",
            excerpt=excerpt,
            detail=detail,
            confidence=confidence,
            linked_metrics=triggered,
        )
        out.append(
            MetricNarrativeMismatch(
                kind=spec.kind,
                detail=detail,
                fiscal_label=label,
                narrative_evidence_id=entry.evidence_id,
                metric_names=triggered,
                metric_values={
                    n: metrics_by_name[n].value  # type: ignore[misc]
                    for n in triggered
                    if n in metrics_by_name and metrics_by_name[n].value is not None
                },
                confidence=confidence,
            )
        )
    return out
