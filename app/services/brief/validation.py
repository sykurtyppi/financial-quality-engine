"""Acceptance checks for model-written earnings briefs.

The writer is interpretive, but the artifact shape is a product contract. A
partial response must fail closed instead of becoming the quarter's record.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.services.brief.assessment import QuarterAssessment, parse_quarter_assessment


REQUIRED_HEADINGS = (
    "Headline",
    "Quarter assessment",
    "Your assumptions",
    "Results vs the company's own prior guidance",
    "Guidance",
    "KPIs and segments",
    "Management framing (release + prepared remarks)",
    "The call",
    "Engine findings worth carrying",
    "Changed since last quarter",
    "Open questions",
    "Sources",
)
ASSUMPTION_VERDICTS = {"held", "challenged", "no news"}


def _section(markdown: str, title: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def _cells(line: str) -> list[str]:
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def _validate_assumptions(markdown: str, expected: Sequence[str]) -> None:
    section = _section(markdown, "Your assumptions")
    if section is None:
        raise ValueError("missing `## Your assumptions` section")

    if not expected:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if (
            len(lines) != 1
            or not lines[0].startswith("UNAVAILABLE")
            or "no standing assumptions" not in lines[0].lower()
        ):
            raise ValueError(
                "without an assumptions source, `Your assumptions` must be one "
                "UNAVAILABLE line"
            )
        return

    rows: list[tuple[int, str, str, str]] = []
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) != 4:
            continue
        number, assumption, verdict, evidence = cells
        if number == "#" or set("".join(cells)) <= {"-", ":", " "}:
            continue
        try:
            index = int(number)
        except ValueError as e:
            raise ValueError(f"invalid assumption row number: {number!r}") from e
        verdict = verdict.lower()
        if verdict not in ASSUMPTION_VERDICTS:
            raise ValueError(f"invalid assumption verdict for row {index}: {verdict!r}")
        if not assumption or not evidence:
            raise ValueError(f"assumption row {index} needs assumption text and evidence")
        rows.append((index, assumption, verdict, evidence))

    expected_numbers = list(range(1, len(expected) + 1))
    if [row[0] for row in rows] != expected_numbers:
        raise ValueError(
            "assumption rows must appear exactly once and in source order: "
            + ", ".join(str(number) for number in expected_numbers)
        )
    for index, assumption, _verdict, _evidence in rows:
        if assumption != expected[index - 1]:
            raise ValueError(f"assumption row {index} does not match its source text")


def validate_brief(
    markdown: str, *, expected_assumptions: Sequence[str]
) -> QuarterAssessment:
    """Validate the fixed brief shape and return its structured assessment."""
    headings = tuple(re.findall(r"^## (.+?)\s*$", markdown, re.MULTILINE))
    if headings != REQUIRED_HEADINGS:
        raise ValueError(
            "brief headings must appear exactly once and in the required order"
        )
    assessment = parse_quarter_assessment(markdown)
    _validate_assumptions(markdown, expected_assumptions)
    return assessment
