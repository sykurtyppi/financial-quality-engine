"""PR 0.5 — every mapper field note reaches the report.

`FieldDiagnostic.notes` records how a scored figure was BUILT ("Current
portion of long-term debt unavailable; total debt may understate", "Only a
depreciation tag was available; amortization may be excluded"). Its only
consumer was `scripts/validate_real_data.py`; the report that scored the
figure never said so. Now `IngestionDiagnostics.field_notes()` feeds the
data-quality appendix through both entry points.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.services.ingestion import restatements
from app.services.ingestion.companyfacts_mapper import (
    FieldDiagnostic,
    IngestionDiagnostics,
    build_dataset,
)
from app.services.reporting.report_builder import build_report, data_quality_section

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"


def _diag(*fields: FieldDiagnostic) -> IngestionDiagnostics:
    return IngestionDiagnostics(
        ticker="T", entity_name=None, quarter_ends=[], fiscal_year_end_month=None,
        fields=list(fields),
    )


def _field(name, notes=()):
    return FieldDiagnostic(field_name=name, tag_used="us-gaap:X", periods_filled=8,
                           periods_total=8, notes=list(notes))


def test_field_notes_are_prefixed_and_in_mapper_order():
    diag = _diag(
        _field("revenue"),
        _field("total_debt", ["Short-term borrowings unavailable or zero; not included.",
                              "Finance-lease liabilities added to total debt; operating leases excluded."]),
        _field("depreciation_amortization", ["Only a depreciation tag was available; amortization may be excluded (capex/D&A can overstate)."]),
    )
    assert diag.field_notes() == [
        "total_debt: Short-term borrowings unavailable or zero; not included.",
        "total_debt: Finance-lease liabilities added to total debt; operating leases excluded.",
        "depreciation_amortization: Only a depreciation tag was available; amortization may be excluded (capex/D&A can overstate).",
    ]
    assert _diag(_field("revenue")).field_notes() == []


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
def test_every_real_fixture_note_appears_verbatim_in_the_report(ticker):
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    ds, diag = build_dataset(facts, ticker, n_quarters=8)
    notes = diag.field_notes()
    assert notes, "fixture produces no notes; the test would be vacuous"
    report, _ = build_report(
        analyze(ds), ds, generated_on=date(2026, 9, 22).isoformat(),
        coverage=diag.coverage(), fetched_at="2026-09-22 09:00 UTC", field_notes=notes,
    )
    for n in notes:
        assert f"- Field note: {n}" in report
    # Notes carry no numbers: the golden's scores are untouched by construction.
    assert "Field note:" not in report.split("## Appendix: Data Acquisition Quality")[0]


def test_a_synthetic_debt_understatement_note_is_rendered():
    """The P0-10 case: a filer with only a noncurrent debt tag. The mapper
    notes that total debt may understate; the report must say so."""
    ends = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    starts = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
    fps = ["Q1", "Q2", "Q3", "Q4"]

    def instant(vals):
        return [{"end": e, "val": v, "filed": "2026-02-01", "form": "10-K", "fy": 2025,
                 "fp": fp, "accn": "A"} for e, v, fp in zip(ends, vals, fps)]

    def flow(vals):
        return [{"start": s, "end": e, "val": v, "filed": "2026-02-01", "form": "10-K",
                 "fy": 2025, "fp": fp, "accn": "A"} for s, e, v, fp in zip(starts, ends, vals, fps)]

    facts = {"facts": {"us-gaap": {
        "Assets": {"units": {"USD": instant([5000.0, 5100.0, 5200.0, 5300.0])}},
        "Revenues": {"units": {"USD": flow([900.0, 950.0, 1000.0, 1050.0])}},
        "LongTermDebtNoncurrent": {"units": {"USD": instant([1000.0, 1000.0, 1000.0, 1000.0])}},
    }}}
    _ds, diag = build_dataset(facts, "T", n_quarters=8)
    notes = diag.field_notes()
    assert "total_debt: Current portion of long-term debt unavailable; total debt may understate." in notes
    section = data_quality_section(fetched_at="x", fresh=False, coverage=0.1, warnings=[],
                                   doc_diagnostics=[], field_notes=notes)
    for n in notes:
        assert f"- Field note: {n}" in section


def test_no_notes_means_no_lines():
    section = data_quality_section(fetched_at="x", fresh=False, coverage=1.0, warnings=[],
                                   doc_diagnostics=[])
    assert "Field note" not in section


def test_both_entry_points_thread_the_notes(monkeypatch, tmp_path):
    """Shape of test_both_entry_points_supply_the_selection: the CLI and the
    journal path both hand build_report the diagnostics' notes."""
    import importlib
    import sys

    from app.services.journal import reporting as journal_reporting
    from tests.fixtures.companies import stretch_dataset

    seen: dict[str, list] = {}
    sentinel = ["total_debt: Short-term borrowings unavailable or zero; not included."]

    class _Diag:
        warnings: list[str] = []

        def coverage(self):
            return 1.0

        def selected_tags(self):
            return {}

        def field_notes(self):
            return list(sentinel)

    class _Snap:
        dataset = stretch_dataset()
        diagnostics = _Diag()
        company_facts = {"facts": {}}

    def fake_build(result, dataset, **kw):
        seen[kw["ticker"]] = kw.get("field_notes")
        from app.services.scoring.thermometer import compute_thermometer
        return "report", compute_thermometer(result.block_scores, dataset.periods)

    for mod in (journal_reporting,):
        monkeypatch.setattr(mod, "fetch_dataset_snapshot", lambda *a, **k: _Snap())
        monkeypatch.setattr(mod, "fetch_submissions_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(mod, "store_vintage_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(mod, "SecClient", lambda **k: object())
        monkeypatch.setattr(mod, "build_full_report", fake_build)
    journal_reporting.build_report("JRN", with_docs=False, out_dir=tmp_path)
    assert seen["JRN"] == sentinel

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("generate_report")
    monkeypatch.setattr(cli, "fetch_dataset_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(cli, "fetch_submissions_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(cli, "store_vintage_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(cli, "SecClient", lambda **k: object())
    monkeypatch.setattr(cli, "build_report", fake_build)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["generate_report.py", "CLI", "--no-docs", "--no-vintage"])
    assert cli.main() == 0
    assert seen["CLI"] == sentinel


def test_the_wrong_composite_helper_is_gone():
    """`is_composite_selection` had no callers and was wrong for the debt
    selection shape (`LongTermDebtNoncurrent+none+none` is not a composite).
    The composite decision lives in `_resolve_tags` / `len(series) > 1`."""
    assert not hasattr(restatements, "is_composite_selection")
