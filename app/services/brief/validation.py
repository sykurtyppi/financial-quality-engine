"""Acceptance checks for model-written earnings briefs.

The writer is interpretive, but the artifact shape is a product contract. A
partial response must fail closed instead of becoming the quarter's record.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.services.brief.assessment import QuarterAssessment, parse_quarter_assessment
from app.services.brief.assumptions import DERIVED, HOLDER
from app.services.brief.text import fold_row, normalize, split_row, unwrap


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

# Engine-derived assumptions sit under a heading that says "Your assumptions",
# so the line disclaiming them is the one thing standing between a machine's
# reading of the filings and a holder who thinks they wrote it. Every other
# property of this section fails closed; this one has to as well. Kept in sync
# with .claude/skills/earnings-brief/SKILL.md.
DERIVED_NOTE = "_Derived from this company's filed history - not your own assumptions._"


def _section(markdown: str, title: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def _plain(line: str) -> str:
    """Normalized, stripped of emphasis and terminal punctuation. Whether the
    writer reached for `_x_`, `*x*` or `**x**` is not the point; the reader
    seeing the disclosure is."""
    return normalize(line).replace("_", "").replace("*", "").strip().rstrip(".").lower()


def _cells(line: str) -> list[str]:
    # Pivot on the verdict cell (held / challenged / no news): a stray pipe in
    # the assumption text and an extra trailing column both produce a surplus,
    # and only the content tells them apart.
    return fold_row(split_row(line), 4, ASSUMPTION_VERDICTS, pivot_at=2)


def _validate_assumptions(markdown: str, expected: Sequence[str], origin: str) -> None:
    section = _section(markdown, "Your assumptions")
    if section is None:
        raise ValueError("missing `## Your assumptions` section")

    if expected and origin == DERIVED:
        opening = next((ln for ln in section.splitlines() if ln.strip()), "")
        if _plain(opening) != _plain(DERIVED_NOTE):
            raise ValueError(
                "assumptions derived from filed history must be disclosed as such: the "
                f"section has to open with `{DERIVED_NOTE}`"
            )

    if not expected:
        lines = [normalize(line) for line in section.splitlines() if line.strip()]
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
        number, verdict = unwrap(number), unwrap(verdict)
        assumption, evidence = normalize(assumption), normalize(evidence)
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
        if assumption != normalize(expected[index - 1]):
            raise ValueError(
                f"assumption row {index} does not match its source text "
                f"(expected {expected[index - 1]!r}, got {assumption!r})")


def validate_brief(
    markdown: str,
    *,
    expected_assumptions: Sequence[str],
    assumptions_origin: str = HOLDER,
) -> QuarterAssessment:
    """Validate the fixed brief shape and return its structured assessment."""
    # Compared normalized: a typographic apostrophe in "the company's own
    # prior guidance" is the same heading to every reader, and rejecting the
    # whole brief over it costs the print.
    headings = tuple(normalize(h) for h in re.findall(r"^## (.+?)\s*$", markdown, re.MULTILINE))
    if headings != tuple(normalize(h) for h in REQUIRED_HEADINGS):
        expected = [h for h in REQUIRED_HEADINGS if normalize(h) not in headings]
        raise ValueError(
            "brief headings must appear exactly once and in the required order"
            + (f" (missing or renamed: {', '.join(expected)})" if expected else "")
        )
    assessment = parse_quarter_assessment(markdown)
    _validate_assumptions(markdown, expected_assumptions, assumptions_origin)
    return assessment
