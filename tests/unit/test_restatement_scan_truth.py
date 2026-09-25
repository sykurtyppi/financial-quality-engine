"""The restatement scan tells the truth about derived quarters and coverage
(Hermes audit round 4, findings 2 and 3).

Finding 2: materiality was applied to filed figures only. A quarter the
engine derives (year-to-date less earlier quarters, the year less three
quarters, a sum of components) is never filed as such, so an H1 revised by
0.9% — below the 1% threshold — moved a derived Q2 from 1 to 1.9 and the
scan said nothing. The mapper is now re-run as of each filing date behind a
derived quarter, and the scored value's own trail is compared.

Finding 3: a field counted as inspected before the period window applied,
so a series whose only facts pre-date the window read "inspected".
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import (
    render_restatements_section,
    scan_restatements,
)
from app.services.reporting.report_builder import _derived_tier1_lines
from tests.fixtures.selection_cases import QUARTER_ENDS, _base, duration, quarter, ytd

Q1, Q2 = QUARTER_ENDS[8], QUARTER_ENDS[9]  # 2024-03-31, 2024-06-30
SINCE, AS_OF = date(2024, 1, 1), date(2025, 6, 30)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "real"


def _ytd_filer(h1_revised: float | None, *, form: str = "10-Q/A", q1_revised: float | None = None):
    """Operating income filed as Q1 (100) and an H1 year-to-date total (101),
    so Q2 is derived: 101 - 100 = 1. Optionally a later filing revises H1
    and/or Q1."""
    p = _base("YTD Co")
    oi = [quarter(e, 90.0 + i) for i, e in enumerate(QUARTER_ENDS[:8])]
    # Filed with each 10-Q's balance sheet (the default filing dates), as a
    # real 10-Q files them together.
    oi.append(quarter(Q1, 100.0))
    oi.append(ytd(Q2, 101.0))
    oi += [quarter(e, 95.0) for e in QUARTER_ENDS[10:]]
    if h1_revised is not None:
        oi.append(ytd(Q2, h1_revised, filed=date(2024, 10, 1), form=form))
    if q1_revised is not None:
        oi.append(quarter(Q1, q1_revised, filed=date(2024, 9, 1), form="10-Q/A"))
    p.add("OperatingIncomeLoss", oi)
    return p.data


def _scan(facts):
    _ds, diag = build_dataset(facts, "T")
    return scan_restatements(
        facts, period_since=SINCE, as_of=AS_OF, selected_tags=diag.selected_tags()
    )


class TestDerivedQuarters:
    def test_a_sub_threshold_revision_that_moves_a_derived_quarter_is_reported(self):
        scan = _scan(_ytd_filer(101.9))
        assert not [f for f in scan.footprints if f.field_name == "operating_income"]
        (d,) = [d for d in scan.derived if d.field_name == "operating_income"]
        assert (d.period_end, d.method) == (Q2, "ytd_diff")
        assert d.original_value == 1.0 and abs(d.current_value - 1.9) < 1e-9
        # `ebit` is scored from the same concept: reported once, like a filed figure.
        assert not [x for x in scan.derived if x.field_name == "ebit"]
        assert (d.original_filed, d.current_filed) == (date(2024, 8, 9), date(2024, 10, 1))
        assert d.period_start == date(2024, 4, 1)
        assert d.is_amendment and ("10-Q/A", d.moved_by[0][1]) == d.moved_by[0]
        (line,) = _derived_tier1_lines(scan.derived)
        assert line.startswith("Restatement (10-Q/A) moved derived operating_income for 2024-06-30 +90.0%")
        text = render_restatements_section(scan)
        assert "### Derived quarters that moved (rebuilt from the filings behind them)" in text
        assert "but 1 derived quarter(s) moved" in text
        assert "No revisions detected" not in text

    def test_a_comparative_revision_is_context_not_tier_1(self):
        scan = _scan(_ytd_filer(101.9, form="10-Q"))
        (d,) = [d for d in scan.derived if d.field_name == "operating_income"]
        assert not d.is_amendment
        assert _derived_tier1_lines(scan.derived) == []

    def test_hermes_q1_only_amendment_moves_nothing_it_is_derived_from(self):
        """Q1 100 -> 100.5 after H1 was filed: H1 still embeds the old Q1, so
        the scored Q2 is 101 - 100 = 1 throughout (R4-A). No footprint —
        correctly: nothing the engine scores moved materially."""
        scan = _scan(_ytd_filer(None, q1_revised=100.5))
        assert not [d for d in scan.derived if d.field_name == "operating_income"]

    def test_a_component_appearing_is_a_change_of_composition_not_a_revision(self):
        p = _base("DA Co")
        p.add("Depreciation", [quarter(e, 20.0) for e in QUARTER_ENDS])
        # Amortization for 2024 Q1 is only reported later, in the 10-K.
        p.add("AmortizationOfIntangibleAssets", [
            quarter(Q1, 10.0, filed=date(2025, 2, 20), form="10-K"),
        ])
        scan = _scan(p.data)
        assert not [d for d in scan.derived if d.field_name == "depreciation_amortization"]

    def test_nothing_derived_nothing_reported(self):
        scan = _scan(_ytd_filer(None))
        assert scan.derived == ()
        assert "Derived quarters" not in render_restatements_section(scan)


class TestCoverageTruth:
    def test_a_series_with_no_period_in_the_window_is_not_inspected(self):
        p = _base("Old Co", revenue=False)
        p.add("Revenues", [duration(date(2019, 1, 1), date(2019, 3, 31), 50.0,
                                    filed=date(2019, 5, 1))])
        scan = scan_restatements(p.data, period_since=SINCE, as_of=AS_OF)
        assert "revenue" not in scan.inspected
        assert scan.uninspected["revenue"] == "selected series has no period on or after 2024-01-01"
        assert scan.incomplete

    def test_a_series_with_a_period_in_the_window_is_inspected(self):
        scan = _scan(_ytd_filer(None))
        assert "operating_income" in scan.inspected


def test_the_rebuild_stays_cheap_on_real_filers():
    for ticker in ("AAPL", "KO", "CRM"):
        facts = json.loads((FIXTURES / f"companyfacts_{ticker}_trimmed.json").read_text())
        _ds, diag = build_dataset(facts, ticker)
        t0 = time.perf_counter()
        scan_restatements(facts, period_since=date(2023, 1, 1), as_of=date(2026, 9, 22),
                          selected_tags=diag.selected_tags())
        assert time.perf_counter() - t0 < 5.0, ticker


def test_a_derived_move_below_materiality_is_not_reported():
    scan = _scan(_ytd_filer(101.005))  # Q2 1 -> 1.005: 0.5%
    assert not [d for d in scan.derived if d.field_name == "operating_income"]


def test_a_period_ending_on_the_window_start_is_in_the_window():
    p = _base("Edge Co", revenue=False)
    p.add("Revenues", [quarter(Q1, 50.0)])
    scan = scan_restatements(p.data, period_since=Q1, as_of=AS_OF)
    assert "revenue" in scan.inspected


def test_the_report_promotes_an_amended_derived_move():
    from app.services.reporting.report_builder import _collect_streams

    class Client:
        def resolve_cik(self, ticker):
            return 1

        def company_facts(self, ticker):
            return facts

        def submissions(self, ticker):
            return {"filings": {"recent": {}}}

        def submissions_by_cik(self, cik):
            return {"filings": {"recent": {}}}

    facts = _ytd_filer(101.9)
    _ds, diag = build_dataset(facts, "T")
    _body, _lines, tier1, errors, *_ = _collect_streams(
        Client(), "T", date(2025, 6, 30), company_facts=facts,
        submissions={"filings": {"recent": {}}}, field_tags=diag.selected_tags(),
    )
    assert errors["restatements"] is None
    assert any(t.startswith("Restatement (10-Q/A) moved derived operating_income") for t in tier1)
