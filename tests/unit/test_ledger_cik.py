"""The evidence ledger names the company's CIK, so its accessions link to EDGAR.

A filing's folder on EDGAR is `/Archives/edgar/data/<CIK>/<accession without
dashes>/`, and the review console can only link an accession there when it
knows the CIK. The ledger used to record none (only an offering row's
document URL embedded one), so on real runs every accession was text.

The CIK comes from what the run already holds — the companyfacts payload's
top-level `cik`, the filing index's `cik`, and the CIK the ticker resolved to
(the offerings stream's) — and is never guessed: sources that disagree, or
one that is not a CIK, leave it None with a note saying which said what.

The trimmed fixtures under tests/fixtures/real carry `entityName` and
`facts` only (no `cik`). The CIKs below are SEC's registry entries for
those entity names (the drill caches' `company_tickers.json` and filing
indexes: 320193 "Apple Inc.", 21344 "COCA COLA CO", 1108524 "Salesforce,
Inc.").
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.pipeline import analyze
from app.schemas.ledger import LedgerDocument
from app.services.ingestion import sec_client, vintages
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.journal import reporting as journal_reporting
from app.services.reporting import ledger as ledger_mod
from app.services.reporting.ledger import build_ledger
from app.services.reporting.report_builder import ledger_path
from scripts import drill

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
CIKS = {"AAPL": 320193, "KO": 21344, "CRM": 1108524}
TITLES = {"AAPL": "Apple Inc.", "KO": "COCA COLA CO", "CRM": "Salesforce, Inc."}
DAY = date(2026, 9, 29)


def _facts(ticker: str) -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


@pytest.fixture
def sec(tmp_path, monkeypatch):
    """An SEC cache seeded as the drill seeds it (registry, companyfacts,
    filing index), read by the real client with the network shut: a run
    that reaches past the cache fails instead of fetching."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(vintages, "VINTAGES", tmp_path / "vintages")
    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path / "reports")

    def no_network(self, url):
        raise sec_client.SecClientError(f"test: no network ({url})")

    monkeypatch.setattr(sec_client.SecClient, "_get", no_network)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: sec_client.SecClient(
        cache_dir=cache, identity="Ledger Test test@example.com"))

    def seed(ticker: str, facts: dict, *, registry_cik: int | None = None) -> Path:
        cik = registry_cik or CIKS[ticker]
        cache.mkdir(exist_ok=True)
        (cache / "company_tickers.json").write_text(json.dumps(
            {"0": {"cik_str": cik, "ticker": ticker, "title": TITLES[ticker]}}))
        (cache / f"companyfacts_CIK{cik:010d}.json").write_text(json.dumps(facts))
        (cache / f"submissions_CIK{cik:010d}.json").write_text(
            json.dumps(drill._index(cik, ticker, date.today())))
        return cache

    return seed


def _ledger(out: Path) -> LedgerDocument:
    return LedgerDocument.model_validate_json(ledger_path(out).read_text())


# --- a real run's ledger ------------------------------------------------------------


@pytest.mark.parametrize("ticker", sorted(CIKS))
@pytest.mark.parametrize("payload_has_cik", [False, True], ids=["trimmed", "as-SEC-serves-it"])
def test_a_real_run_ledger_carries_the_company_cik(sec, ticker, payload_has_cik):
    """The live path (`journal.reporting.build_report`, the same builder the
    CLI calls): the fixture scored, the index and registry from the cache.
    SEC's payload carries `cik` as an int; the trimmed fixture does not."""
    facts = _facts(ticker)
    if payload_has_cik:
        facts = {"cik": CIKS[ticker], **facts}
    sec(ticker, facts)
    out, _ = journal_reporting.build_report(ticker, with_docs=False, vintage=False)
    doc = _ledger(out)
    assert doc.cik == CIKS[ticker]
    assert doc.cik_note is None
    # ...and it survives the round trip the console reads it through.
    assert json.loads(ledger_path(out).read_text())["cik"] == CIKS[ticker]


def test_a_replay_ledger_carries_the_stored_snapshots_cik(sec):
    """--as-of: the fundamentals are the stored snapshot's payload, and the
    CIK is read from it (with the index and registry agreeing)."""
    facts = {"cik": CIKS["KO"], **_facts("KO")}
    sec("KO", _facts("KO"))  # today's cached payload: no cik of its own
    vintages.store_snapshot(CIKS["KO"], facts, now=datetime.now(UTC) - timedelta(days=1))
    out, _ = journal_reporting.build_report(
        "KO", with_docs=False, report_day=date.today().isoformat(), replay=True)
    assert out.name.endswith(".replay.md")
    doc = _ledger(out)
    assert doc.cik == CIKS["KO"] and doc.cik_note is None


def test_a_replay_whose_snapshot_names_another_cik_records_none_and_says_why(sec):
    """The stored payload says one CIK, the registry and the index another:
    the ledger picks neither, and the note names each source's value."""
    facts = {"cik": 999, **_facts("KO")}
    sec("KO", _facts("KO"))
    vintages.store_snapshot(CIKS["KO"], facts, now=datetime.now(UTC) - timedelta(days=1))
    out, _ = journal_reporting.build_report(
        "KO", with_docs=False, report_day=date.today().isoformat(), replay=True)
    doc = _ledger(out)
    assert doc.cik is None
    assert doc.cik_note is not None
    assert "999" in doc.cik_note and str(CIKS["KO"]) in doc.cik_note
    assert "companyfacts payload" in doc.cik_note


def test_a_live_payload_that_disagrees_with_the_registry_records_none(sec):
    """The payload's own `cik` against the CIK the ticker resolved to."""
    sec("CRM", {"cik": 21344, **_facts("CRM")})
    out, _ = journal_reporting.build_report("CRM", with_docs=False, vintage=False)
    doc = _ledger(out)
    assert doc.cik is None
    assert "21344" in (doc.cik_note or "") and str(CIKS["CRM"]) in (doc.cik_note or "")


def test_the_cli_live_path_records_the_cik(sec, tmp_path, monkeypatch):
    """scripts/generate_report.py, the operator's command."""
    from scripts import generate_report

    cache = sec("AAPL", _facts("AAPL"))
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: sec_client.SecClient(
        cache_dir=cache, identity="Ledger Test test@example.com"))
    monkeypatch.setattr("sys.argv", ["generate_report.py", "AAPL", "--no-docs", "--no-vintage"])
    assert generate_report.main() == 0
    (out,) = (tmp_path / "reports").glob("AAPL_*.ledger.json")
    assert LedgerDocument.model_validate_json(out.read_text()).cik == CIKS["AAPL"]


# --- the builder ----------------------------------------------------------------------


@pytest.mark.parametrize(("sources", "cik"), [
    ({"a": 21344}, 21344),
    ({"a": 21344, "b": "21344"}, 21344),
    ({"a": 21344, "b": "0000021344"}, 21344),  # the index's zero-padded form
    ({"a": None, "b": "1108524"}, 1108524),
    ({}, None),
    ({"a": None, "b": None}, None),
])
def test_agreeing_or_absent_sources(sources, cik):
    assert ledger_mod.ledger_cik(sources) == (cik, None)


@pytest.mark.parametrize("sources", [
    {"a": 21344, "b": 320193},
    {"a": 21344, "b": "320193"},
    {"a": "21344", "b": None, "c": 21345},
    {"a": "abc"},                       # not a CIK at all
    {"a": 21344, "b": "abc"},
    {"a": True},                        # a bool is not a CIK
    {"a": 0},
    {"a": -21344},
    {"a": "12345678901"},               # eleven digits
    {"a": 21344.0},
])
def test_disagreeing_or_unreadable_sources_record_none_and_say_so(sources):
    cik, note = ledger_mod.ledger_cik(sources)
    assert cik is None
    assert note is not None
    for name, value in sources.items():
        if value is not None:
            assert f"{name} says {value!r}" in note


def _offering(cik: int) -> SimpleNamespace:
    filing = SimpleNamespace(
        form="424B5", filing_date=date(2026, 8, 1), accession="0000021344-26-000010",
        primary_doc="p.htm", kind="takedown", security_type="equity", excerpt="",
    )
    return SimpleNamespace(cik=cik, filings=[filing])


def _ko():
    ds, _ = build_dataset(_facts("KO"), "KO")
    return ds, analyze(ds)


def test_the_offering_row_link_uses_the_ledgers_cik():
    ds, result = _ko()
    doc = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY,
                       streams={"ran": True, "offerings": _offering(21344)}, errors={},
                       cik_sources={"the companyfacts payload": 21344,
                                    "the filing index": "21344"})
    assert doc.cik == 21344
    (row,) = [i for i in doc.items if i.kind == "offering"]
    assert row.provenance[0].url == \
        "https://www.sec.gov/Archives/edgar/data/21344/000002134426000010/p.htm"


def test_when_the_resolved_cik_disagrees_the_offering_row_is_not_linked_either():
    """The offering URL was built from the resolved CIK alone: against a
    payload naming another, it must not link where the ledger will not."""
    ds, result = _ko()
    doc = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY,
                       streams={"ran": True, "offerings": _offering(21344)}, errors={},
                       cik_sources={"the companyfacts payload": 77})
    assert doc.cik is None and "77" in (doc.cik_note or "")
    (row,) = [i for i in doc.items if i.kind == "offering"]
    assert row.provenance[0].url is None


def test_a_ledger_built_without_sec_data_names_no_cik():
    """The API and tests: no payload, no index, no client — unknown, not guessed."""
    ds, result = _ko()
    doc = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY)
    assert doc.cik is None and doc.cik_note is None


# --- the schema ---------------------------------------------------------------------


def test_an_older_ledger_without_the_field_still_loads():
    ds, result = _ko()
    new = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY,
                       cik_sources={"the companyfacts payload": 21344})
    assert new.cik == 21344
    raw = json.loads(new.model_dump_json())
    assert raw["cik"] == 21344
    old = {k: v for k, v in raw.items() if k not in ("cik", "cik_note")}
    loaded = LedgerDocument.model_validate_json(json.dumps(old))
    assert loaded.cik is None and loaded.cik_note is None
    assert loaded.items == new.items
    # A written ledger round-trips whole, the CIK included.
    assert LedgerDocument.model_validate_json(new.model_dump_json()) == new
