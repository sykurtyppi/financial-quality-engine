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

    @property
    def is_ok(self) -> bool:
        return self.status is MetricStatus.OK
