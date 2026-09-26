"""The evidence ledger's contract: an item names its filings or the items it
derives from, never nothing; a claim that cannot be sourced is listed, not
dropped; the report's entry points write the ledger beside the report."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.pipeline import analyze
from app.schemas.ledger import (
    EvidenceItem,
    LedgerDocument,
    Plane,
    Provenance,
    ValidationStatus,
)
from app.services.backtesting.events import fetch_entity_events
from app.services.ingestion.fields import FIELDS
from app.services.metrics_registry import (
    BASIS,
    FIELD_WINDOWS,
    FINANCIAL_METRICS,
    SERIES_OF,
    USABLE_CAPEX_INTENSITY,
    Basis,
)
from app.services.narrative.evidence import NOT_LOCATED
from app.services.reporting.ledger import _cited, _id, build_ledger
from tests.fixtures.companies import stretch_dataset

DAY = date(2026, 9, 22)
FILING = Provenance(accession="0000320193-26-000001", form="10-Q", filed=date(2026, 5, 1))


def _item(**kw) -> EvidenceItem:
    base = dict(id="EV-1", plane=Plane.ACCOUNTING, kind="metric", subject="dso",
                claim="dso computed", validation_status=ValidationStatus.DIRECTIONAL)
    return EvidenceItem(**{**base, **kw})


# --- schema -----------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["accession", "form", "filed"])
def test_a_filing_source_must_identify_the_filing(missing):
    kw = dict(accession="a", form="10-Q", filed=date(2026, 5, 1))
    kw[missing] = None
    with pytest.raises(ValidationError, match="accession, form and filed"):
        Provenance(**kw)


def test_a_snapshot_source_must_identify_the_snapshot():
    Provenance(kind="snapshot", snapshot_sha256="ab" * 32, captured=DAY)
    with pytest.raises(ValidationError, match="snapshot_sha256 and captured"):
        Provenance(kind="snapshot", snapshot_sha256="ab" * 32)
    for sign in (0, 2, -2):  # a source is added or subtracted, nothing else
        with pytest.raises(ValidationError, match="sign"):
            Provenance(accession="a", form="10-Q", filed=DAY, sign=sign)
    assert Provenance(accession="a", form="10-Q", filed=DAY, sign=-1).sign == -1


def test_an_item_resting_on_nothing_cannot_be_constructed():
    _item(provenance=(FILING,))
    _item(derived_from=("EV-0",))
    with pytest.raises(ValidationError, match="names no source"):
        _item()


def test_the_document_rejects_duplicate_ids_and_dangling_derivations():
    a = _item(provenance=(FILING,))
    LedgerDocument(ticker="X", generated_on=DAY, config_version="0.4.0",
                   items=[a, _item(id="EV-2", derived_from=("EV-1",))])
    with pytest.raises(ValidationError, match="duplicate evidence ids"):
        LedgerDocument(ticker="X", generated_on=DAY, config_version="0.4.0", items=[a, a])
    with pytest.raises(ValidationError, match="unknown items"):
        LedgerDocument(ticker="X", generated_on=DAY, config_version="0.4.0",
                       items=[_item(id="EV-2", derived_from=("EV-9",))])


def test_the_document_round_trips_through_json():
    doc = LedgerDocument(ticker="X", generated_on=DAY, config_version="0.4.0",
                         items=[_item(provenance=(FILING,), inputs={"a": 1.0, "b": None})])
    assert LedgerDocument.model_validate_json(doc.model_dump_json()) == doc


def test_ids_are_content_derived():
    assert _id("metric", "dso", "FY2026Q1") == _id("metric", "dso", "FY2026Q1")
    assert _id("metric", "dso", "FY2026Q1") != _id("metric", "dso", "FY2026Q2")
    assert _id("metric", "dso", "FY2026Q1").startswith("EV-")


# --- citing narrative documents -------------------------------------------------


def _docs():
    ds = stretch_dataset()
    return [
        d.model_copy(update=dict(source=f"8-K 000{i} EX-99.1", accession=f"000{i}", form="8-K",
                                 filed=date(2026, 1 + i, 5)))
        for i, d in enumerate(ds.documents)
    ]


def test_a_rows_source_string_resolves_to_its_documents():
    docs = _docs()
    assert _cited("8-K 0001 EX-99.1", docs) == [docs[1]]
    assert _cited("8-K 0002 EX-99.1; 8-K 0000 EX-99.1", docs) == [docs[0], docs[2]]
    derived = ("derived from FY2025Q4 documents: 8-K 0003 EX-99.1; "
               "compared with FY2025Q3 documents: 8-K 0002 EX-99.1")
    assert _cited(derived, docs) == [docs[2], docs[3]]
    assert _cited(NOT_LOCATED, docs) == []
    assert _cited("derived from FY2025Q4 documents (no source recorded)", docs) == []


def _stretch_with_sources():
    ds = stretch_dataset()
    ds.documents = _docs()
    return ds


def test_a_mismatch_derives_from_its_narrative_row_and_unsourced_metrics_are_listed():
    """The golden dataset is hand-built, not mapped from companyfacts: its
    metrics carry no per-value provenance and must be listed as unsourced —
    not dropped, not given a guessed filing. Its narrative rows cite the
    documents, and the mismatch derives from its row."""
    ds = _stretch_with_sources()
    result = analyze(ds)
    doc = build_ledger(result=result, dataset=ds, ticker="stretch", report_date=DAY)
    [mismatch] = [i for i in doc.items if i.kind == "mismatch"]
    assert mismatch.plane is Plane.CONSISTENCY and mismatch.derived_from
    sources = [doc.item(d) for d in mismatch.derived_from]
    assert all(s.kind == "narrative_evidence" and s.provenance for s in sources)
    financial = {u.subject for u in doc.unsourced if u.kind == "metric"}
    assert financial == {e.metric_name for e in result.evidence
                         if e.metric_name in FINANCIAL_METRICS}
    assert all("per-value provenance" in u.reason for u in doc.unsourced)
    assert doc.streams == dict.fromkeys(
        ("offerings", "restatements", "events", "filing_events", "vintage"), "not run"
    )


def test_validation_follows_the_cards_tiers():
    ds = _stretch_with_sources()
    doc = build_ledger(result=analyze(ds), dataset=ds, ticker="stretch", report_date=DAY)
    status = {(i.kind, i.subject): i.validation_status for i in doc.items}
    assert status[("metric", "kpi_removals")] is ValidationStatus.UNVALIDATED
    assert status[("narrative_evidence", "adjustment_recurrence")] is ValidationStatus.DIRECTIONAL


def test_a_row_not_located_cites_its_periods_documents_and_says_so():
    ds = _stretch_with_sources()
    result = analyze(ds)
    row = result.narrative_evidence[0].model_copy(update=dict(source=NOT_LOCATED))
    result = result.model_copy(update=dict(narrative_evidence=[row], mismatches=[]))
    doc = build_ledger(result=result, dataset=ds, ticker="stretch", report_date=DAY)
    [item] = [i for i in doc.items if i.kind == "narrative_evidence"]
    period = {d.accession for d in ds.documents if d.fiscal_label == row.fiscal_label}
    assert set(item.accessions()) == period
    assert "provenance lists the period's documents" in item.note
    assert all(p.excerpt is None for p in item.provenance)  # not claimed as the quote's home


# --- inputs the ledger needs ----------------------------------------------------


def test_every_series_metric_names_its_base_metric():
    assert set(SERIES_OF) == {n for n, b in BASIS.items() if b is Basis.SERIES}
    assert {base for base, _select in SERIES_OF.values()} <= FINANCIAL_METRICS
    # and every metric read from period fields names the fields it reads
    assert set(FIELD_WINDOWS) == {n for n, b in BASIS.items() if b is Basis.FIELDS}
    for spec in FIELD_WINDOWS.values():
        assert spec == USABLE_CAPEX_INTENSITY or set(spec) <= {f.name for f in FIELDS}


def test_an_8k_402_keeps_its_accession():
    subs = {"filings": {"recent": {
        "form": ["8-K", "8-K"], "items": ["4.02", "2.02"],
        "filingDate": ["2026-04-03", "2026-05-01"], "accessionNumber": ["acc-1", "acc-2"],
    }}}
    events = fetch_entity_events(SimpleNamespace(), "T", submissions=subs)
    assert events.non_reliance_8k_filings == ((date(2026, 4, 3), "acc-1", "8-K"),)
    assert events.non_reliance_8k_dates == [date(2026, 4, 3)]


# --- both entry points write it beside the report --------------------------------


def test_both_entry_points_write_the_ledger_beside_the_report(monkeypatch, tmp_path):
    from app.core.pipeline import analyze as real_analyze
    from app.services.journal import reporting as journal_reporting
    from scripts import generate_report

    observed: list = []

    def fake_build(*args, **kwargs):
        observed.append(kwargs["ledger_out"])
        kwargs["ledger_out"].write_text('{"ledger": true}')
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    diagnostics = SimpleNamespace(coverage=lambda: 1.0, warnings=[], selected_tags=lambda: {},
                                  selected_series=lambda: {}, field_notes=lambda: [])
    snap = SimpleNamespace(dataset=stretch_dataset(), diagnostics=diagnostics,
                           company_facts={"facts": {}})

    class _C:
        def submissions(self, t):
            return {"filings": {"recent": {}}}

        def resolve_cik(self, t):
            return 1

    for mod, builder in ((generate_report, "build_report"), (journal_reporting, "build_full_report")):
        monkeypatch.setattr(mod, "SecClient", lambda *a, **k: _C())
        monkeypatch.setattr(mod, "fetch_dataset_snapshot", lambda *a, **k: snap)
        monkeypatch.setattr(mod, "analyze", real_analyze)
        monkeypatch.setattr(mod, "store_vintage_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(mod, builder, fake_build)
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL", "--no-docs"])
    assert generate_report.main() == 0
    out, _ = journal_reporting.build_report("aapl", with_docs=False, out_dir=tmp_path / "j",
                                            report_day="2026-09-01")

    # Each builder writes into staging (Hermes round 8: a rebuild is built off
    # to the side), and the ledger is published beside the report it belongs to.
    cli, journal = observed
    assert cli.parent == tmp_path / "reports" / ".staging" and cli.name.endswith(".ledger.json")
    assert journal.parent == tmp_path / "j" / ".staging"
    (cli_report,) = (tmp_path / "reports").glob("AAPL_*.md")
    assert cli_report.with_suffix(".ledger.json").read_text() == '{"ledger": true}'
    assert out.with_suffix(".ledger.json").read_text() == '{"ledger": true}'
    assert not cli.exists() and not journal.exists()


def test_ids_do_not_depend_on_what_else_is_in_the_ledger():
    import json
    from pathlib import Path

    from app.services.ingestion.companyfacts_mapper import build_dataset

    facts = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "real"
                        / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    result = analyze(ds)
    full = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY)
    fewer = build_ledger(
        result=result.model_copy(update=dict(evidence=result.evidence[3:])),
        dataset=ds, ticker="KO", report_date=DAY,
    )
    by_subject = {(i.subject, i.fiscal_label): i.id for i in full.items}
    assert all(by_subject[(i.subject, i.fiscal_label)] == i.id for i in fewer.items)
    assert len(fewer.items) == len(full.items) - 3


def test_a_failed_offerings_stream_contributes_no_items():
    from app.services.reporting.report_builder import StreamFailure

    filing = SimpleNamespace(
        form="424B5", filing_date=date(2026, 8, 1), accession="acc-1", primary_doc="p.htm",
        kind="takedown", security_type="equity", excerpt="",
    )
    ds = _stretch_with_sources()
    streams = {"ran": True, "offerings": SimpleNamespace(cik=1, filings=[filing])}
    ok = build_ledger(result=analyze(ds), dataset=ds, ticker="X", report_date=DAY,
                      streams=streams, errors={})
    assert [i.kind for i in ok.items].count("offering") == 1
    failed = build_ledger(
        result=analyze(ds), dataset=ds, ticker="X", report_date=DAY, streams=streams,
        errors={"offerings": StreamFailure("data", "submissions outage")},
    )
    assert "offering" not in {i.kind for i in failed.items}
    assert failed.streams["offerings"] == "data failure: submissions outage"


def test_a_derived_quarter_that_moved_is_an_item_cited_by_the_filings_that_moved_it():
    """Hermes audit round 4: a derived quarter can move materially while no
    raw fact does (a 0.9% H1 revision moves a Q2 of 1 to 1.9). The scan
    reports it; the ledger cites the filings made the day it moved."""
    from app.services.ingestion.restatements import DerivedRevision

    def dr(end, moved_by):
        return DerivedRevision(
            field_name="revenue", period_start=date(2026, 4, 1), period_end=end,
            method="ytd_diff", original_value=1.0, original_filed=date(2026, 8, 1),
            current_value=1.9, current_filed=date(2026, 9, 1), moved_by=moved_by,
        )

    amended = dr(date(2026, 6, 30), (("10-Q/A", "0000320193-26-000009"),))
    comparative = dr(date(2025, 6, 30), (("10-Q", "0000320193-26-000008"),))
    unidentified = dr(date(2024, 6, 30), (("10-Q", ""),))
    ds = stretch_dataset()
    doc = build_ledger(
        result=analyze(ds), dataset=ds, ticker="X", report_date=DAY,
        streams={"restatements": SimpleNamespace(footprints=[],
                                                 derived=(amended, comparative, unidentified))},
        errors={},
    )
    items = [i for i in doc.items if i.kind == "derived_revision"]
    assert len(items) == 2
    by_state = {i.change_state: i for i in items}
    a, c = by_state["amended"], by_state["revised"]
    assert a.validation_status is ValidationStatus.VALIDATED
    assert c.validation_status is ValidationStatus.DIRECTIONAL
    (p,) = a.provenance
    assert (p.accession, p.form, p.filed) == ("0000320193-26-000009", "10-Q/A", date(2026, 9, 1))
    assert (p.period_end, p.value, p.method) == (date(2026, 6, 30), 1.9, "ytd_diff")
    assert "2026-08-01" in a.claim and "2026-09-01" in a.claim
    # A derived move whose filing is not fully identified is listed, not dropped.
    assert [u.kind for u in doc.unsourced if u.kind == "derived_revision"] == ["derived_revision"]
