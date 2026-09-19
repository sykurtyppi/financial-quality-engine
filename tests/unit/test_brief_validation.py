from __future__ import annotations

import pytest

from app.services.brief.validation import validate_brief
from tests.unit.test_earnings_brief import _valid_brief


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
