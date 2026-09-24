"""The provenance-completeness gate: every claim a report makes is in its
evidence ledger, and every item names filings that exist.

A run is built on each real fixture with every evidence stream populated: a
10-Q/A re-filing one quarter's facts (restatement footprints), two stored
snapshots across it (silent revisions), a shelf and a takedown (offerings),
an 8-K Item 4.02, and earnings releases with filing identities (narrative
evidence). The ledger must then hold one sourced item per claim, list
nothing as unsourced, cite only accessions present in what the run read, and
rebuild each metric input from the facts it cites.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.schemas.ledger import LedgerDocument, ValidationStatus
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.offerings import fetch_offerings
from app.services.ingestion.restatements import scan_restatements
from app.services.ingestion.vintages import report_diff, store_snapshot
from app.services.reporting import report_builder
from app.services.reporting.decision_card import tier_of
from app.services.reporting.report_builder import build_report, ledger_path
from tests.fixtures.companies import stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
CIK = 320193
DAY = date(2026, 9, 22)
AMENDMENT = "0000320193-26-900001"
_PROSPECTUS = (
    "<html><body>PROSPECTUS SUPPLEMENT 10,000,000 Shares of Common Stock offered "
    "by the selling stockholders at $12.50 per share</body></html>"
)


def _submissions() -> dict:
    # The second 4.02 predates the report's event window: the card does not
    # list it, so neither may the ledger.
    forms = ["424B5", "S-3", "8-K", "10-Q", "8-K"]
    return {"cik": str(CIK), "name": "T", "sic": "3571", "sicDescription": "x",
            "filings": {"recent": {
                "form": forms,
                "filingDate": ["2026-08-01", "2026-05-02", "2026-04-03", "2026-03-04",
                               "2023-01-10"],
                "accessionNumber": [f"0000320193-26-80000{i}" for i in range(len(forms))],
                "primaryDocument": ["p424.htm", "s3.htm", "8k.htm", "q.htm", "8k0.htm"],
                "items": ["", "", "4.02,9.01", "", "4.02"],
            }}}


class _Client:
    def __init__(self, facts: dict):
        self._facts = facts

    def resolve_cik(self, ticker):
        return CIK

    def company_facts(self, ticker):
        return self._facts

    def company_facts_by_cik(self, cik):
        return self._facts

    def submissions(self, ticker):
        return _submissions()

    def submissions_by_cik(self, cik):
        return _submissions()

    def _get(self, url):
        return _PROSPECTUS.encode()

    def archive_text(self, cik, accession, doc, *, honor_fresh=True):
        return _PROSPECTUS


def _amended(facts: dict, end: str) -> dict:
    out = copy.deepcopy(facts)
    for tags in out["facts"].values():
        for concept in tags.values():
            for rows in concept["units"].values():
                rows += [
                    dict(r, val=r["val"] * 1.1, filed="2026-08-15", form="10-Q/A", accn=AMENDMENT)
                    for r in list(rows) if r.get("end") == end and "val" in r
                ]
    return out


def _run(ticker: str, tmp_path: Path, *, ledger: bool = True):
    base = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    ds0, _ = build_dataset(base, ticker)
    facts = _amended(base, ds0.sorted_periods()[-2].period_end.isoformat())
    ds, diag = build_dataset(facts, ticker)
    # The golden narrative documents, moved onto this filer's last four
    # quarters and given the filing identity the ingester records.
    labels = [p.fiscal_label for p in ds.sorted_periods()][-4:]
    stretch = stretch_dataset()
    relabel = dict(zip(sorted({d.fiscal_label for d in stretch.documents}), labels))
    ds.documents = [
        d.model_copy(update=dict(
            fiscal_label=relabel[d.fiscal_label], source=f"8-K 0000320193-26-7000{i:02d} EX-99.1",
            accession=f"0000320193-26-7000{i:02d}", form="8-K", filed=date(2026, 1 + i, 5),
        ))
        for i, d in enumerate(stretch.documents)
    ]
    result = analyze(ds)
    root = tmp_path / "vintages"
    if not root.exists():  # a second run reads the same store
        store_snapshot(CIK, base, now=datetime(2026, 9, 1, tzinfo=UTC), root=root)
        store_snapshot(CIK, facts, now=datetime(2026, 9, 20, tzinfo=UTC), root=root)
    out = tmp_path / f"{ticker}_{DAY}.ledger.json"
    report, _ = build_report(
        result, ds, generated_on=DAY.isoformat(), coverage=diag.coverage(),
        field_tags=diag.selected_series(), client=_Client(facts), ticker=ticker,
        fetched_at="2026-09-22 09:00 UTC", company_facts=facts, submissions=_submissions(),
        field_notes=diag.field_notes(), vintage_note="captured", vintage_root=root,
        ledger_out=out if ledger else None,
    )
    doc = LedgerDocument.model_validate_json(out.read_text()) if ledger else None
    return doc, report, result, ds, diag, facts, root


def _known_accessions(facts: dict, ds) -> set[str]:
    out = {r["accn"] for tags in facts["facts"].values() for c in tags.values()
           for rows in c["units"].values() for r in rows if r.get("accn")}
    out |= set(_submissions()["filings"]["recent"]["accessionNumber"])
    out |= {d.accession for d in ds.documents if d.accession}
    return out


TICKERS = ["AAPL", "KO", "CRM"]


@pytest.mark.parametrize("ticker", TICKERS)
def test_every_claim_is_sourced_and_every_source_exists(ticker, tmp_path):
    doc, _report, result, ds, diag, facts, root = _run(ticker, tmp_path)
    assert doc.unsourced == []
    assert set(doc.streams.values()) == {"checked"}
    kinds = {(i.plane.value, i.kind) for i in doc.items}
    assert {
        ("accounting", "metric"), ("accounting", "restatement_footprint"),
        ("accounting", "silent_revision"), ("narrative", "narrative_evidence"),
        ("narrative", "metric"), ("capital_markets", "offering"),
        ("filing_behavior", "non_reliance_8k_402"),
    } <= kinds

    known = _known_accessions(facts, ds)
    for item in doc.items:
        assert item.provenance or item.derived_from
        for p in item.provenance:
            if p.kind == "filing":
                assert p.accession in known, (item.kind, item.subject, p.accession)

    # One item per claim the run made, stream by stream.
    by_kind = defaultdict(list)
    for item in doc.items:
        by_kind[item.kind].append(item)
    assert {(i.subject, i.fiscal_label) for i in by_kind["metric"]} == {
        (e.metric_name, e.fiscal_label) for e in result.evidence
    }
    assert len(by_kind["narrative_evidence"]) == len(result.narrative_evidence)
    scan = scan_restatements(facts, period_since=date(DAY.year - 3, 1, 1), as_of=DAY,
                             selected_tags=diag.selected_series())
    assert len(by_kind["restatement_footprint"]) == len(scan.footprints) > 0
    assert all(i.validation_status is ValidationStatus.VALIDATED
               for i in by_kind["restatement_footprint"])  # every one is a 10-Q/A
    rep = report_diff(CIK, as_of=DAY, since=date(DAY.year - 3, 1, 1), root=root)
    assert len(by_kind["silent_revision"]) == len(rep.changes_since_previous) > 0
    timeline = fetch_offerings(_Client(facts), ticker, as_of=DAY, submissions=_submissions())
    assert len(by_kind["offering"]) == len(timeline.filings) == 2
    [nr] = by_kind["non_reliance_8k_402"]  # the one inside the window, as on the card
    assert nr.accessions() == ["0000320193-26-800002"]
    assert "8-K Item 4.02 non-reliance (restatement announced) filed 2026-04-03" in _report
    assert "filed 2023-01-10" not in _report
    assert nr.validation_status is ValidationStatus.VALIDATED


@pytest.mark.parametrize("ticker", TICKERS)
def test_a_metrics_cited_facts_rebuild_its_inputs(ticker, tmp_path):
    doc, *_ = _run(ticker, tmp_path)
    checked = 0
    for item in doc.items:
        if item.kind != "metric" or item.plane.value != "accounting":
            continue
        assert item.validation_status is ValidationStatus(
            {1: "validated", 2: "directional", 3: "unvalidated"}[tier_of({item.subject})]
        )
        totals: dict[str, float] = defaultdict(float)
        for p in item.provenance:
            totals[p.role] += p.sign * p.value
        for role, total in totals.items():
            if role in item.inputs and item.inputs[role] is not None:
                assert math.isclose(total, item.inputs[role], rel_tol=1e-9, abs_tol=1e-6), (
                    item.subject, role,
                )
                checked += 1
    assert checked > 60


def test_the_ledger_is_stable_and_the_report_does_not_depend_on_it(tmp_path):
    first, report, *_ = _run("KO", tmp_path)
    again, report_again, *_ = _run("KO", tmp_path)
    assert first == again
    _none, without, *_ = _run("KO", tmp_path, ledger=False)
    assert report == report_again == without


def test_a_ledger_failure_costs_the_ledger_not_the_report(monkeypatch, tmp_path, caplog):
    import app.services.reporting.ledger as ledger_mod

    def boom(**kw):
        raise RuntimeError("ledger defect")

    monkeypatch.setattr(ledger_mod, "build_ledger", boom)
    with pytest.raises(RuntimeError, match="ledger defect"):  # strict in tests
        _run("AAPL", tmp_path)

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", False)
    stale = tmp_path / f"AAPL_{DAY}.ledger.json"
    stale.write_text("{}")  # an earlier run's ledger
    with caplog.at_level(logging.ERROR):
        _doc, report, *_ = _run("AAPL", tmp_path, ledger=False)
        ds = stretch_dataset()
        path = report_builder.write_ledger(
            stale, result=analyze(ds), dataset=ds, ticker="AAPL", report_date=DAY,
        )
    assert path is None and not stale.exists()
    assert "evidence ledger" in caplog.text and report


def test_ledger_path_sits_beside_the_report():
    assert ledger_path(Path("reports/AAPL_2026-09-24.md")) == Path(
        "reports/AAPL_2026-09-24.ledger.json"
    )
