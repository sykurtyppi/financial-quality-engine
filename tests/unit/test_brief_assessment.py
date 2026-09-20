from __future__ import annotations

import pytest

from app.services.brief.assessment import AssessmentRead, parse_quarter_assessment


VALID = """# TEST earnings brief

## Headline
Revenue landed above the company's prior range.

## Quarter assessment
| Dimension | Read | Evidence |
|---|---|---|
| Results vs prior guidance | favorable | prior_release + release: revenue above the range |
| Forward guidance | mixed | release: revenue raised; margin held |
| Operating KPIs | not assessable | not in supplied sources |
| Cash and earnings quality | unfavorable | report: CFO trailed net income |
| Balance sheet and capital | favorable | report: net debt declined |

**Overall earnings read:** mixed
**Investment context:** not assessed - price, valuation, expectations, and the user's required return are separate from whether the quarter was good.

## Guidance
Raised.
"""


def test_parses_complete_assessment_in_contract_order():
    result = parse_quarter_assessment(VALID)

    assert result.overall == AssessmentRead.MIXED
    assert [row.key for row in result.dimensions] == [
        "results_vs_prior_guidance",
        "forward_guidance",
        "operating_kpis",
        "cash_and_earnings_quality",
        "balance_sheet_and_capital",
    ]
    assert result.dimensions[0].read == AssessmentRead.FAVORABLE


@pytest.mark.parametrize(
    "old,new,match",
    [
        ("## Quarter assessment", "## Other", "missing"),
        ("| Forward guidance | mixed |", "| Forward guidance | strong |", "invalid"),
        ("| Operating KPIs | not assessable | not in supplied sources |\n", "", "exactly once"),
        ("**Investment context:** not assessed", "**Investment context:** attractive", "not assessed"),
    ],
)
def test_rejects_incomplete_or_investment_rating_output(old, new, match):
    with pytest.raises(ValueError, match=match):
        parse_quarter_assessment(VALID.replace(old, new))


def test_json_contract_contains_only_descriptive_reads():
    payload = parse_quarter_assessment(VALID).model_dump(mode="json")
    assert payload["overall"] == "mixed"
    assert {row["read"] for row in payload["dimensions"]} <= {
        "favorable", "mixed", "unfavorable", "not assessable"
    }


def test_rejects_recommendation_appended_to_required_disclaimer():
    brief = VALID.replace(
        "whether the quarter was good.",
        "whether the quarter was good; this is a strong buy.",
    )
    with pytest.raises(ValueError, match="exactly state"):
        parse_quarter_assessment(brief)
