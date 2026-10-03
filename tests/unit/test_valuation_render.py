"""The shadow card's text: every line carries its data class, the empty
cases say why, and nothing is printed as a number that is not one."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

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
from app.services.valuation.render import render_valuation_section
from tests.fixtures.companies import stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DAY = date(2026, 10, 3)
ENDS = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))


def _obs(ticker: str, **kw) -> MarketObservation:
    return MarketObservation(ticker=ticker, price=66.25, currency="USD",
                             observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC), source="test close",
                             recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC), **kw)


def _section(ds: CompanyDataset, obs: MarketObservation) -> str:
    return render_valuation_section(compute_plane(ds, LoadedObservation.of(obs), DAY))


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
    assert re.search(r"- \[D\] Gordon implied perpetual FCF growth: [+-]\d+\.\d%/yr \(g = r − FCF_ttm / EV\)", s)
    assert "- [D] the main assumption that would change the conclusion: " in s
    assert "- [A] scenarios: no scenarios recorded" in s


def test_without_provenance_the_filing_cell_says_so():
    s = _section(stretch_dataset(), _obs("STRETCHCO"))
    assert "| (no per-value provenance) |" in s
    assert "- [O] note:" not in s
    assert "availability not checked" in s


def test_the_empty_cases_say_why():
    s = _section(_dataset(net_income=-300.0, cfo=0.0, total_debt=None), _obs("SYN"))
    assert "| **enterprise value** | D | — | FY2025Q4 | EV not asserted: total_debt missing for FY2025Q4 |" in s
    assert "| P/E | — | | | TTM net income is negative (-1,200): P/E undefined |" in s
    assert "| EV/EBIT | — | | | EV not asserted: total_debt missing for FY2025Q4 |" in s
    assert re.search(r"\| P/S \| \d+\.\d\dx \|", s)
    assert "- [D] Gordon implied perpetual FCF growth: implied growth not computable: TTM FCF ≤ 0 (g = r − FCF_ttm / EV)" in s
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
