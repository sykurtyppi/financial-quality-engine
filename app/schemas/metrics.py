"""Metric result contract: every computed value carries its formula, inputs,
and an explicit status so results are traceable and missing data is never
silently dropped."""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="after")
    def _finite_value(self) -> MetricResult:
        """NaN or +/-inf is not a measurement (Hermes audit of 424b0b4,
        finding 7). An OK result carrying one reached every consumer as a
        number: NaN compares False against everything, so the journal
        resolver proposed `violated`, and the scoring engine's interpolation
        matched no anchor segment and raised; +/-inf was clamped to an
        extreme concern as if it were an extreme ratio. Here it becomes
        NOT_MEANINGFUL with no value — undefined for these inputs, the
        status every consumer already handles — and the note says why.
        `distress_signal` is left as given: whether an undefined ratio means
        distress is the formula's call, not this check's.

        A non-OK result's value is not read by the engine, but a non-finite
        one is dropped all the same so no consumer meets it. `inputs` are
        left as they are: they are the diagnostic record of what the formula
        was given — a non-finite input is exactly the explanation a reader
        needs — and nothing scores or resolves on them.

        Runs on construction and on `model_validate` — and again, in place,
        whenever pydantic re-validates an instance placed in another model
        (a `MetricsBundle`), which is why it must stay idempotent. Not on
        `model_copy(update=...)` or `model_construct`, which pydantic never
        validates; the registry's copies change notes and missing fields
        only, and the journal resolver re-checks what it compares."""
        if self.value is not None and not math.isfinite(self.value):
            if self.status is MetricStatus.OK:
                why = f"value is not a finite number ({self.value!r})"
                self.note = why if self.note is None else f"{self.note}; {why}"
                self.status = MetricStatus.NOT_MEANINGFUL
            self.value = None
        return self

    @property
    def is_ok(self) -> bool:
        return self.status is MetricStatus.OK
