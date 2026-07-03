"""Score result contracts. All scores are 0-100 CONCERN scores:
0 = no concern, 100 = maximum concern. Direction and confidence are derived,
never asserted independently of the underlying metrics."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    POSITIVE = "positive"
    MIXED = "mixed"
    NEGATIVE = "negative"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ComponentContribution(BaseModel):
    """One metric's contribution to a block score — full transparency."""

    metric_name: str
    metric_value: float | None
    concern_score: float | None = Field(description="0-100 mapping of this metric")
    weight: float = Field(description="Weight within the block (pre-normalization)")
    anchors: list[tuple[float, float]] = Field(
        description="(metric_value, concern_score) anchor points used for the mapping"
    )
    status: str
    note: str | None = None


class BlockScore(BaseModel):
    name: str
    score: float | None = Field(description="0-100 concern score; None if insufficient data")
    direction: Direction
    confidence: Confidence
    rationale: str
    components: list[ComponentContribution]
    caveats: list[str] = Field(default_factory=list)
    data_coverage: float = Field(description="Fraction of block metrics that computed OK")


class OverallScore(BaseModel):
    score: float | None
    direction: Direction
    confidence: Confidence
    block_weights: dict[str, float] = Field(
        description="v0 heuristic weights — NOT backtested; see docs/scoring_methodology.md"
    )
    rationale: str
    caveats: list[str] = Field(default_factory=list)
