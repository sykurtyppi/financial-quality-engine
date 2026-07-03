"""Top-level analysis result: everything the reporting layer needs,
machine-readable."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.financials import CompanyProfile
from app.schemas.metrics import MetricResult
from app.schemas.scoring import BlockScore, OverallScore


class Flag(BaseModel):
    """A red or green flag, always tied to specific metric evidence."""

    severity: str = Field(description='"red" or "green"')
    title: str
    detail: str
    evidence_metrics: list[str] = Field(description="Metric names backing this flag")
    fiscal_label: str


class EvidenceEntry(BaseModel):
    """One row of the evidence ledger: claim -> formula -> values -> period."""

    claim: str
    metric_name: str
    formula: str
    inputs: dict[str, float | None]
    value: float | None
    fiscal_label: str


class NarrativeFinding(BaseModel):
    """Deterministic narrative-engine finding with source evidence."""

    kind: str = Field(description="e.g. adjustment_recurrence, kpi_removed, kpi_added")
    detail: str
    fiscal_label: str
    evidence_snippets: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    profile: CompanyProfile
    analyzed_periods: list[str]
    excluded: bool = False
    exclusion_reason: str | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    block_scores: list[BlockScore] = Field(default_factory=list)
    overall: OverallScore | None = None
    red_flags: list[Flag] = Field(default_factory=list)
    green_flags: list[Flag] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list, description="What changed this period")
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    narrative_findings: list[NarrativeFinding] = Field(default_factory=list)
    analyst_questions: list[str] = Field(default_factory=list)
