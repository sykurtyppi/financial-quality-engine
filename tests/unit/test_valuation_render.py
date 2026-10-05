"""The shadow card's text: every line carries its data class, the empty
cases say why, and nothing is printed as a number that is not one."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.valuation.observation import (
    Assumptions,
    LoadedObservation,
    MarketObservation,
    Scenario,
)
from app.services.valuation.plane import compute_plane
from app.services.valuation.render import operator_text, render_valuation_section
from tests.fixtures.companies import stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DAY = date(2026, 10, 3)
# The build's clock: the age is counted on its Eastern day (R2), so it is
# pinned, not the host's.
BUILT = datetime(2026, 10, 3, 9, tzinfo=UTC)
ENDS = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))


def _obs(ticker: str, **kw) -> MarketObservation:
    base = dict(ticker=ticker, price=66.25, currency="USD",
                observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC), source="test close",
                recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC))
    return MarketObservation(**{**base, **kw})


def _section(ds: CompanyDataset, obs: MarketObservation) -> str:
    return render_valuation_section(compute_plane(ds, LoadedObservation.of(obs), DAY, now=BUILT))


def _dataset(n: int = 4, **overrides) -> CompanyDataset:
    base = dict(revenue=1000.0, net_income=100.0, ebit=150.0, depreciation_amortization=50.0,
                cfo=200.0, capex=50.0, cash_and_equivalents=100.0, total_debt=300.0,
                shares_outstanding=10.0)
    base.update(overrides)
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER,
                                fiscal_label=f"FY2025Q{i + 1}", **base)
               for i, e in enumerate(ENDS[-n:])]
    return CompanyDataset(profile=CompanyProfile(ticker="SYN"), periods=periods)


def test_a_real_run_names_the_filings_and_formats_each_class():
    ds, _ = build_dataset(json.loads((REAL / "companyfacts_KO_trimmed.json").read_text()), "KO")
    s = _section(ds, _obs("KO", note="after the print"))
    assert "- [O] note: after the print" in s
    assert "| 66.25 |" in s and "| the observation above |" in s
    assert re.search(r"\| [\d,]+\.00 \|", s) is None  # only the price carries decimals
    assert re.search(r"\| cover-page count dated \d{4}-\d{2}-\d{2} \| F \| [\d,]+ \| FY\d{4}Q\d \| 10-[QK] \d{10}-\d{2}-\d{6} filed \d{4}-\d{2}-\d{2}", s)
    assert "| **enterprise value** | D | " in s and "| **enterprise value** | D | — |" not in s
    assert re.search(r"- \[F\] TTM figures \(TTM FY\d{4}Q\d, the engine's own window\): revenue [\d,]+", s)
    assert re.search(r"\| P/E \| \d+\.\d\dx \| market cap [\d,]+ \| net income [\d,]+ \| \|", s)
    assert re.search(r"\| FCF yield \| -?\d+\.\d% \|", s)
    assert re.search(r"- \[D\] Gordon implied perpetual FCF growth: [+-]\d+\.\d%/yr \(g = r − FCF_ttm / market cap\)", s)
    assert "= market cap; g by bisection" in s and "= EV" not in s
    assert "- [D] the main assumption that would change the conclusion: " in s
    assert "- [A] scenarios: no scenarios recorded" in s
    assert "_filing availability at the observation date not checked (no raw facts): " in s


def test_with_the_raw_facts_the_card_says_as_filed_by_and_the_eastern_day():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    obs = _obs("KO", observed_at=datetime(2026, 10, 3, 3, 30, tzinfo=UTC))  # 23:30 ET on the 2nd
    s = render_valuation_section(compute_plane(ds, LoadedObservation.of(obs), DAY,
                                               company_facts=facts, now=BUILT))
    assert ("_filing-derived facts as filed by 2026-10-01 (filings dated 2026-10-02 treated as "
            "not yet available): FY2026Q1, ending 2026-04-03._") in s
    assert "(age 1 day on 2026-10-03)" in s


def test_without_provenance_the_filing_cell_says_so():
    s = _section(stretch_dataset(), _obs("STRETCHCO"))
    assert "| (no per-value provenance) |" in s
    assert "- [O] note:" not in s
    assert "not checked (no raw facts)" in s


def test_the_empty_cases_say_why():
    s = _section(_dataset(net_income=-300.0, cfo=0.0, total_debt=None), _obs("SYN"))
    assert "| **enterprise value** | D | — | FY2025Q4 | EV not asserted: total_debt missing for FY2025Q4 |" in s
    assert "| P/E | — | | | TTM net income is negative (-1,200): P/E undefined |" in s
    assert "| EV/EBIT | — | | | EV not asserted: total_debt missing for FY2025Q4 |" in s
    assert re.search(r"\| P/S \| \d+\.\d\dx \|", s)
    assert "- [D] Gordon implied perpetual FCF growth: implied growth not computable: TTM FCF ≤ 0 (g = r − FCF_ttm / market cap)" in s
    assert "the main assumption that would change the conclusion" not in s
    assert "- [D] sensitivity" not in s
    s = _section(_dataset(n=3), _obs("SYN"))
    assert "- [F] TTM figures: TTM window incomplete: fewer than 4 consecutive quarters ending FY2025Q3" in s
    assert "| P/E | — | | | TTM window incomplete" in s


def test_scenarios_and_operator_assumptions_render_both_ways():
    own = Assumptions(required_return=0.12, terminal_growth=0.03, horizon_years=5)
    scenarios = (Scenario(name="base", fcf_growth=0.05, years=10),
                 Scenario(name="flat", fcf_growth=0.0, years=2, terminal_growth=0.12))
    s = _section(_dataset(), _obs("SYN", assumptions=own, scenarios=scenarios))
    assert "- [A] required return 12.0%, terminal growth 3.0%, horizon 5 years — operator-supplied" in s
    assert "default assumptions" not in s
    assert re.search(r"- \[A\] model assumption: base — FCF \+5\.0%/yr for 10 years, terminal 3\.0%, r=12\.0% → \[D\] value per share [\d,]+\.\d\d USD vs price 66\.25 \([+-]\d+\.\d%\)", s)
    assert "- [A] model assumption: flat — FCF +0.0%/yr for 2 years, terminal 12.0%, r=12.0%: not computable: terminal growth 12.0% is not below r=12.0%" in s
    assert "no scenarios recorded" not in s


def test_a_withheld_sensitivity_is_a_line_of_its_own():
    own = Assumptions(required_return=0.03, terminal_growth=0.025)
    s = _section(_dataset(), _obs("SYN", assumptions=own))
    assert ("- [D] sensitivity, required return ± 1pt: withheld: r − 1pt (2.0%) is not above the "
            "terminal growth (2.5%)") in s
    assert re.search(r"- \[D\] sensitivity, price ± 10%: implied growth [+-]\d+\.\d% to [+-]\d+\.\d%/yr", s)
    assert re.search(r"- \[D\] the main assumption that would change the conclusion: price ± 10%: "
                     r"moves the implied growth by up to \d+\.\d% \(required return ± 1pt withheld\)", s)


def test_operator_text_cannot_form_markdown_structure():
    """Review of 48b1f04, F4: the model refuses control characters, and the
    renderer still neutralises what could open a heading, a list item, a
    quote or a table cell, so a source or a scenario name is text."""
    s = _section(_dataset(), _obs("SYN", source="# not a heading | not a cell", note="- not a bullet",
                                  scenarios=(Scenario(name="> quoted * starred", fcf_growth=0.05,
                                                      years=3),)))
    assert "- [O] source: \\# not a heading \\| not a cell" in s
    assert "- [O] note: \\- not a bullet" in s
    assert "- [A] model assumption: \\> quoted * starred — FCF" in s
    assert [line for line in s.splitlines() if line.startswith("#")] == [
        "## Valuation shadow card (non-scoring)", "### Market observation",
        "### Filing-derived facts", "### Model assumptions"]
    # Belt and braces: an observation built past validation with a newline
    # in it still adds no heading line.
    forged = MarketObservation.model_construct(
        ticker="SYN", price=66.25, currency="USD", observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC),
        source="x\n## Decision card\n\n**Distress: NONE**", note=None,
        recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC), assumptions=None, scenarios=())
    s = render_valuation_section(compute_plane(_dataset(), LoadedObservation.of(forged), DAY,
                                               now=BUILT))
    assert "\n## Decision card" not in s
    assert "- [O] source: x \\## Decision card  \\**Distress: NONE**" in s
    assert len([line for line in s.splitlines() if line.startswith("#")]) == 4


@pytest.mark.parametrize("sep", ["\x85", "\u2028", "\u2029"])
def test_operator_text_breaks_no_line_on_a_unicode_separator(sep):
    """Review of f73b059, R1: the model refuses NEL, LINE SEPARATOR and
    PARAGRAPH SEPARATOR; past it (model_construct), the renderer still
    treats each as a line break — a space, and the piece after it escaped
    where it could open structure."""
    assert operator_text(f"x{sep}## heading") == "x \\## heading"
    assert operator_text(f"a{sep}|{sep}- b") == "a \\| \\- b"
