"""Aggregates narrative-engine outputs into MetricResults so the scoring
engine can consume them under the same traceability contract as financial
formulas, plus a disclosure-volume trend."""

from __future__ import annotations

from app.schemas.financials import DocumentRecord
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import NarrativeFinding
from app.services.narrative import adjustment_language, kpi_drift


def _missing(name: str, formula: str, reason: str) -> MetricResult:
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label="n/a",
        status=MetricStatus.MISSING_DATA,
        missing_fields=[reason],
    )


def disclosure_volume_change(documents: list[DocumentRecord]) -> MetricResult:
    """Latest period total word count vs mean of prior periods. Values well
    below 1.0 indicate shrinking disclosure."""
    by_label: dict[str, int] = {}
    order: list[str] = []
    for doc in documents:
        if doc.fiscal_label not in by_label:
            by_label[doc.fiscal_label] = 0
            order.append(doc.fiscal_label)
        by_label[doc.fiscal_label] += len(doc.text.split())
    formula = "latest period word count / mean(prior period word counts)"
    if len(order) < 3:
        return _missing("disclosure_volume_change", formula, "documents for >= 3 periods")
    latest = by_label[order[-1]]
    prior = [by_label[label] for label in order[:-1]]
    prior_mean = sum(prior) / len(prior)
    if prior_mean == 0:
        return _missing("disclosure_volume_change", formula, "non-empty prior documents")
    return MetricResult(
        name="disclosure_volume_change",
        formula=formula,
        fiscal_label=order[-1],
        status=MetricStatus.OK,
        value=latest / prior_mean,
        inputs={"latest_words": float(latest), "prior_mean_words": prior_mean},
    )


def compute_narrative_metrics(
    documents: list[DocumentRecord],
) -> tuple[list[MetricResult], list[NarrativeFinding]]:
    if not documents:
        return (
            [
                _missing(
                    "adjustment_recurrence_ratio",
                    "periods with adjustment language / periods analyzed",
                    "no documents provided",
                ),
                _missing(
                    "recurring_adjustment_terms",
                    "count of terms appearing in >= 3 periods",
                    "no documents provided",
                ),
                _missing("kpi_removals", "KPIs in prior periods absent from latest", "no documents provided"),
                _missing(
                    "disclosure_volume_change",
                    "latest period word count / mean(prior period word counts)",
                    "no documents provided",
                ),
            ],
            [],
        )

    adj = adjustment_language.analyze(documents)
    kpi = kpi_drift.analyze(documents)
    latest_label = documents[-1].fiscal_label
    metrics: list[MetricResult] = []

    if adj.periods_analyzed >= 3:
        metrics.append(
            MetricResult(
                name="adjustment_recurrence_ratio",
                formula="periods with adjustment language / periods analyzed",
                fiscal_label=latest_label,
                status=MetricStatus.OK,
                value=adj.recurrence_ratio,
                inputs={"periods_analyzed": float(adj.periods_analyzed)},
            )
        )
        metrics.append(
            MetricResult(
                name="recurring_adjustment_terms",
                formula="count of terms appearing in >= 3 periods",
                fiscal_label=latest_label,
                status=MetricStatus.OK,
                value=float(len(adj.recurring_terms)),
                inputs={t: float(c) for t, c in adj.recurring_terms.items()},
            )
        )
    else:
        metrics.append(
            _missing(
                "adjustment_recurrence_ratio",
                "periods with adjustment language / periods analyzed",
                "documents for >= 3 periods",
            )
        )
        metrics.append(
            _missing(
                "recurring_adjustment_terms",
                "count of terms appearing in >= 3 periods",
                "documents for >= 3 periods",
            )
        )

    if kpi.periods_analyzed >= 2:
        metrics.append(
            MetricResult(
                name="kpi_removals",
                formula="KPIs discussed in prior periods absent from latest",
                fiscal_label=latest_label,
                status=MetricStatus.OK,
                value=float(len(kpi.removed)),
                inputs={},
                note=("Removed: " + ", ".join(sorted(kpi.removed))) if kpi.removed else None,
            )
        )
    else:
        metrics.append(
            _missing("kpi_removals", "KPIs in prior periods absent from latest", "documents for >= 2 periods")
        )

    metrics.append(disclosure_volume_change(documents))
    findings = adj.findings + kpi.findings
    return metrics, findings
