"""The brief contract must reject a WRONG brief and accept a merely
differently-typed one.

Every check here is exact-string against model output, and a rejected brief
is retried a few times and then the print simply has none. So each case below
is a way a correct brief could have been thrown away.
"""

from __future__ import annotations

import pytest

from app.services.brief import assumptions as asm
from app.services.brief.assessment import parse_quarter_assessment
from app.services.brief.validation import validate_brief
from tests.unit._brief_fixtures import valid_brief


class TestAcceptsTypographicVariation:
    def test_smart_apostrophe_in_a_heading(self):
        brief = valid_brief().replace("the company's own", "the company’s own")
        assert validate_brief(brief, expected_assumptions=[]).overall.value == "mixed"

    def test_em_dash_and_nonbreaking_space(self):
        brief = valid_brief().replace("not assessed - price",
                                      "not assessed — price")
        assert validate_brief(brief, expected_assumptions=[])

    def test_bolded_table_cells(self):
        brief = (valid_brief(assumptions=1)
                 .replace("| Results vs prior guidance |", "| **Results vs prior guidance** |")
                 .replace("| favorable |", "| **favorable** |")
                 .replace("| held |", "| **held** |"))
        assert validate_brief(brief, expected_assumptions=["DC revenue grows"])

    def test_not_assessable_is_rejected_for_investment_context(self):
        brief = valid_brief().replace("**Investment context:** not assessed -",
                                      "**Investment context:** not assessable -")
        with pytest.raises(ValueError, match="not assessed"):
            validate_brief(brief, expected_assumptions=[])

    def test_overall_read_with_a_trailing_period(self):
        brief = valid_brief().replace("**Overall earnings read:** mixed",
                                      "**Overall earnings read:** mixed.")
        assert parse_quarter_assessment(brief).overall.value == "mixed"


class TestAcceptsMarkdownHabits:
    def test_escaped_underscore_in_an_assumption(self):
        brief = valid_brief(assumptions=1).replace("DC revenue grows", "DC\\_revenue grows")
        assert validate_brief(brief, expected_assumptions=["DC_revenue grows"])

    def test_an_extra_column_in_the_assessment_table(self):
        brief = valid_brief().replace(
            "| Forward guidance | mixed | release: revenue raised; margin held |",
            "| Forward guidance | mixed | release: revenue raised; margin held | medium |")
        assert parse_quarter_assessment(brief).dimensions[1].read.value == "mixed"

    def test_a_pipe_in_assessment_evidence(self):
        brief = valid_brief().replace(
            "| Forward guidance | mixed | release: revenue raised; margin held |",
            "| Forward guidance | mixed | release: revenue up | margin flat |")
        rows = parse_quarter_assessment(brief).dimensions
        assert rows[1].read.value == "mixed" and "margin flat" in rows[1].evidence

    def test_an_extra_column_does_not_drop_the_row(self):
        brief = valid_brief(assumptions=1).replace(
            "| 1 | DC revenue grows | held | release: DC revenue grew |",
            "| 1 | DC revenue grows | held | release: DC revenue grew | high |")
        assert validate_brief(brief, expected_assumptions=["DC revenue grows"])


class TestPipesCannotCostAPrint:
    def test_a_pipe_is_neutralized_where_the_assumption_is_written(self, tmp_path):
        # Both sides of the comparison come from here, so neither can carry a
        # raw pipe into a table cell.
        asm.add_assumption("NVDA", "Mix stays: cloud | on-prem near 70/30", root=tmp_path)
        assert asm.load_assumptions("NVDA", root=tmp_path) == [
            "Mix stays: cloud / on-prem near 70/30"]
        assert asm.parse_assumptions("- a | b\n") == ["a / b"]

    def test_an_unescaped_pipe_in_a_cell_still_parses(self):
        # Belt and braces: a hand-edited file from before the rule, or a model
        # that writes one anyway, must not drop the row.
        brief = valid_brief(assumptions=1).replace(
            "| 1 | DC revenue grows | held | release: DC revenue grew |",
            "| 1 | cloud | on-prem mix holds | held | release: mix 72/28 |")
        assert validate_brief(brief, expected_assumptions=["cloud | on-prem mix holds"])


class TestStillRejectsWrongBriefs:
    """Tolerance must not become permissiveness."""

    def test_a_reworded_assumption_is_still_rejected(self):
        brief = valid_brief(assumptions=1).replace("DC revenue grows", "Margins expand")
        with pytest.raises(ValueError, match="source text"):
            validate_brief(brief, expected_assumptions=["DC revenue grows"])

    def test_an_invented_verdict_is_still_rejected(self):
        brief = valid_brief(assumptions=1).replace("| held |", "| probably |")
        with pytest.raises(ValueError, match="invalid assumption verdict"):
            validate_brief(brief, expected_assumptions=["DC revenue grows"])

    def test_an_invented_read_is_still_rejected(self):
        brief = valid_brief().replace("| Forward guidance | mixed |",
                                      "| Forward guidance | strong |")
        with pytest.raises(ValueError, match="invalid assessment read"):
            validate_brief(brief, expected_assumptions=[])

    def test_an_investment_recommendation_is_still_rejected(self):
        brief = valid_brief().replace("**Investment context:** not assessed -",
                                      "**Investment context:** attractive entry -")
        with pytest.raises(ValueError, match="not assessed"):
            validate_brief(brief, expected_assumptions=[])

    def test_a_missing_heading_names_what_is_missing(self):
        brief = valid_brief().replace("## Guidance\n", "")
        with pytest.raises(ValueError, match="Guidance"):
            validate_brief(brief, expected_assumptions=[])

    def test_a_dropped_assumption_row_is_still_rejected(self):
        brief = valid_brief(assumptions=2).replace(
            "| 2 | No dilution | no news | not in supplied sources |\n", "")
        with pytest.raises(ValueError, match="source order"):
            validate_brief(brief, expected_assumptions=["DC revenue grows", "No dilution"])
