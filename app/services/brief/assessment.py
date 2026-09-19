"""Structured, source-grounded read of an earnings brief.

The brief is still prose for a human, but this section has a deliberately
small contract so a later web/API surface can render it without parsing an
LLM's adjectives. It describes the print; it does not rate the investment.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.services.brief.text import fold_row, normalize, split_row, unwrap


class AssessmentRead(str, Enum):
    FAVORABLE = "favorable"
    MIXED = "mixed"
    UNFAVORABLE = "unfavorable"
    NOT_ASSESSABLE = "not assessable"


DIMENSIONS = {
    "results_vs_prior_guidance": "Results vs prior guidance",
    "forward_guidance": "Forward guidance",
    "operating_kpis": "Operating KPIs",
    "cash_and_earnings_quality": "Cash and earnings quality",
    "balance_sheet_and_capital": "Balance sheet and capital",
}
_LABEL_TO_KEY = {label.lower(): key for key, label in DIMENSIONS.items()}


class AssessmentDimension(BaseModel):
    key: str
    label: str
    read: AssessmentRead
    evidence: str = Field(min_length=1)


class QuarterAssessment(BaseModel):
    overall: AssessmentRead
    dimensions: list[AssessmentDimension]

    @model_validator(mode="after")
    def _complete_once(self) -> "QuarterAssessment":
        keys = [item.key for item in self.dimensions]
        expected = list(DIMENSIONS)
        if keys != expected:
            raise ValueError(
                "quarter assessment dimensions must appear exactly once and in order: "
                + ", ".join(DIMENSIONS.values())
            )
        return self


def normalize_lines(section: str) -> str:
    """Per-line normalization that keeps line structure (the two `**...:**`
    lines are matched with `^...$`)."""
    return "\n".join(normalize(line) for line in section.splitlines())


def _section(markdown: str, title: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def _table_cells(line: str) -> list[str]:
    # Pivot on the `Read` cell: it is a closed vocabulary, so a stray pipe in
    # the label or the evidence cannot be confused with an added column.
    return fold_row(split_row(line), 3, {r.value for r in AssessmentRead}, pivot_at=1)


def parse_quarter_assessment(markdown: str) -> QuarterAssessment:
    """Parse and validate the fixed ``## Quarter assessment`` section.

    Raises ValueError when the model omitted a dimension, invented a read, or
    blurred the print assessment into an investment recommendation.
    """
    section = _section(markdown, "Quarter assessment")
    if section is None:
        raise ValueError("missing `## Quarter assessment` section")

    dimensions: list[AssessmentDimension] = []
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _table_cells(line)
        if len(cells) != 3:
            continue
        label, read_text, evidence = (unwrap(c) for c in cells)
        if label.lower() == "dimension" or set("".join(cells)) <= {"-", ":", " "}:
            continue
        key = _LABEL_TO_KEY.get(label.lower().rstrip("."))
        if key is None:
            raise ValueError(f"unknown quarter-assessment dimension: {label!r}")
        try:
            read = AssessmentRead(read_text.lower())
        except ValueError as e:  # noqa: PERF203
            raise ValueError(f"invalid assessment read for {label}: {read_text!r}") from e
        if not evidence:
            raise ValueError(f"missing evidence for quarter-assessment dimension: {label}")
        dimensions.append(
            AssessmentDimension(key=key, label=DIMENSIONS[key], read=read, evidence=evidence)
        )

    section = normalize_lines(section)
    overall_match = re.search(
        r"^\*\*Overall earnings read:\*\*\s*([^\n]+)$", section, re.MULTILINE
    )
    if overall_match is None:
        raise ValueError("missing `**Overall earnings read:**` line")
    try:
        overall = AssessmentRead(unwrap(overall_match.group(1)).lower().rstrip("."))
    except ValueError as e:
        raise ValueError(
            f"invalid overall earnings read: {overall_match.group(1).strip()!r}"
        ) from e

    investment_line = re.search(r"^\*\*Investment context:\*\*\s*(.+)$", section, re.MULTILINE)
    # "not assessable" is accepted alongside "not assessed": the dimension
    # reads directly above use that exact word up to six times, so a model
    # autocompleting the same token here is writing the same meaning.
    said = normalize(investment_line.group(1)).lower() if investment_line else ""
    if not any(p in said for p in ("not assessed", "not assessable")):
        raise ValueError("investment context must be present and explicitly `not assessed`")

    return QuarterAssessment(overall=overall, dimensions=dimensions)
