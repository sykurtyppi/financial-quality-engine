"""Shared brief fixture: the smallest brief that satisfies the contract.

Lives outside the test modules so validation, assessment and tolerance
tests can share it without importing each other.
"""

from __future__ import annotations


def valid_brief(headline: str = "ok", assumptions: int = 0) -> str:
    assumption_rows = {
        0: "UNAVAILABLE - no standing assumptions on file.\n",
        1: "| # | Assumption | Verdict | Evidence |\n|---|---|---|---|\n"
           "| 1 | DC revenue grows | held | release: DC revenue grew |\n",
        2: "| # | Assumption | Verdict | Evidence |\n|---|---|---|---|\n"
           "| 1 | DC revenue grows | held | release: DC revenue grew |\n"
           "| 2 | No dilution | no news | not in supplied sources |\n",
    }[assumptions]
    return f"""# NVDA earnings brief
## Headline
{headline}
## Quarter assessment
| Dimension | Read | Evidence |
|---|---|---|
| Results vs prior guidance | favorable | release: revenue above prior range |
| Forward guidance | mixed | release: revenue raised; margin held |
| Operating KPIs | not assessable | not in supplied sources |
| Cash and earnings quality | not assessable | not in supplied sources |
| Balance sheet and capital | not assessable | not in supplied sources |
**Overall earnings read:** mixed
**Investment context:** not assessed - valuation and expectations are separate.
## Your assumptions
{assumption_rows}## Results vs the company's own prior guidance
Revenue was above the prior range.
## Guidance
Mixed.
## KPIs and segments
UNAVAILABLE
## Management framing (release + prepared remarks)
UNAVAILABLE
## The call
UNAVAILABLE
## Engine findings worth carrying
UNAVAILABLE
## Changed since last quarter
UNAVAILABLE
## Open questions
- None.
## Sources
- release
"""
