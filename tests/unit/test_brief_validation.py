from __future__ import annotations

import pytest

from app.services.brief.validation import validate_brief
from tests.unit._brief_fixtures import valid_brief as _valid_brief


def test_accepts_complete_brief_without_assumptions():
    result = validate_brief(_valid_brief(), expected_assumptions=[])
    assert result.overall.value == "mixed"


def test_rejects_missing_required_heading():
    brief = _valid_brief().replace("## Guidance\n", "")
    with pytest.raises(ValueError, match="headings"):
        validate_brief(brief, expected_assumptions=[])


def test_rejects_missing_assumption_row():
    brief = _valid_brief(assumptions=2).replace(
        "| 2 | No dilution | no news | not in supplied sources |\n", ""
    )
    with pytest.raises(ValueError, match="source order"):
        validate_brief(
            brief, expected_assumptions=["DC revenue grows", "No dilution"]
        )


def test_rejects_invented_assumption_verdict():
    brief = _valid_brief(assumptions=1).replace("| held |", "| confirmed |")
    with pytest.raises(ValueError, match="invalid assumption verdict"):
        validate_brief(brief, expected_assumptions=["DC revenue grows"])


def test_rejects_assumption_text_not_found_in_source():
    brief = _valid_brief(assumptions=1).replace("DC revenue grows", "Margins expand")
    with pytest.raises(ValueError, match="source text"):
        validate_brief(brief, expected_assumptions=["DC revenue grows"])


def test_requires_unavailable_line_when_no_assumptions_exist():
    brief = _valid_brief().replace(
        "UNAVAILABLE - no standing assumptions on file.\n", "Nothing to report.\n"
    )
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        validate_brief(brief, expected_assumptions=[])


class TestDerivedProvenance:
    """The heading says "Your assumptions". When the engine wrote them, the one
    line saying so is what stops a machine's reading of the filings from
    becoming something the holder believes they wrote — so it fails closed like
    every other property of this section."""

    NOTE = "_Derived from this company's filed history - not your own assumptions._"

    def _with(self, opening: str) -> str:
        brief = _valid_brief(assumptions=1)
        return brief.replace("## Your assumptions\n", f"## Your assumptions\n{opening}\n")

    def test_accepts_a_disclosed_derived_section(self):
        validate_brief(self._with(self.NOTE), expected_assumptions=["DC revenue grows"],
                       assumptions_origin="derived")

    def test_rejects_a_derived_section_with_no_disclosure(self):
        with pytest.raises(ValueError, match="derived from filed history"):
            validate_brief(_valid_brief(assumptions=1),
                           expected_assumptions=["DC revenue grows"],
                           assumptions_origin="derived")

    def test_rejects_a_disclosure_buried_below_the_table(self):
        brief = _valid_brief(assumptions=1).replace(
            "## Results vs", f"{self.NOTE}\n\n## Results vs")
        with pytest.raises(ValueError, match="derived from filed history"):
            validate_brief(brief, expected_assumptions=["DC revenue grows"],
                           assumptions_origin="derived")

    @pytest.mark.parametrize("written", [
        "_Derived from this company's filed history — not your own assumptions._",
        "*Derived from this company's filed history - not your own assumptions.*",
        "**Derived from this company's filed history - not your own assumptions**",
    ])
    def test_tolerates_how_the_line_was_emphasised(self, written):
        # A brief rejected over an em dash or a bold marker costs the print,
        # and the reader sees the same sentence either way.
        validate_brief(self._with(written), expected_assumptions=["DC revenue grows"],
                       assumptions_origin="derived")

    def test_holder_authored_sections_need_no_such_line(self):
        validate_brief(_valid_brief(assumptions=1), expected_assumptions=["DC revenue grows"])
