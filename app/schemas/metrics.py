"""Metric result contract: every computed value carries its formula, inputs,
and an explicit status so results are traceable and missing data is never
silently dropped."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MetricStatus(str, Enum):
    OK = "ok"
    MISSING_DATA = "missing_data"
    NOT_MEANINGFUL = "not_meaningful"


class MetricResult(BaseModel):
    name: str
    formula: str = Field(description="Human-readable formula definition")
    fiscal_label: str
    value: float | None = None
    status: MetricStatus
    inputs: dict[str, float | None] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    note: str | None = None
    distress_signal: bool = Field(
        default=False,
        description=(
            "P0-9: the ratio is NOT_MEANINGFUL because its denominator crossed "
            "into distress territory (e.g. negative EBITDA with net debt, a loss "
            "with cash burn). The scoring engine treats this as maximum concern "
            "instead of dropping the metric — a distress state must never lift "
            "the block score by handing weight to less-alarming survivors."
        ),
    )

    @property
    def is_ok(self) -> bool:
        return self.status is MetricStatus.OK
