"""The earnings-night review console (`/review`): supervised shadow runs.

The engine is cleared only for supervised shadow runs, with every surfaced
fact reconciled by hand to its accession before it is relied on. The console
is where that happens, and these tests hold it to three things:

- it reads one run WHOLE: the report, its ledger and its audit through
  `report_files.read_live`, and a ledger or audit of another run is said,
  never shown as this run's;
- a reviewer's tick is bound to the run it was made on (its generation id
  and the ledger row's content-derived id): a rebuild does not inherit it,
  and a tick for a run that is not live is refused;
- its one write (the ticks) stays in the journal's own folder, under a lock,
  whole or not at all, and never through a symlink planted in its way.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.ledger import (
    EvidenceItem,
    LedgerDocument,
    Plane,
    Provenance,
    Unsourced,
)
from app.services.journal import reporting, store
from app.services.reporting import report_files
from app.services.reporting.report_files import (
    ENGINE_ENV,
    current_generation,
    engine_commit,
    recording,
    replacing,
    restore,
)
from app.services.watch import watchlist as wl
from app.web import app

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
KO_CIK = 21344
# A filing of the KO fixture's own, the source of the offering row below.
KO_DOC = "https://www.sec.gov/Archives/edgar/data/21344/000002134426000010/q.htm"
XSS = "<script>alert('ledger')</script>"


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    """Publishes here state a fixed engine commit, not this checkout's."""
    monkeypatch.setenv(ENGINE_ENV, "0123456789ab")
    engine_commit.cache_clear()
    yield
    engine_commit.cache_clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The journal, the reports and the watchlist, all under tmp_path."""
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "journal" / "entries")
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(wl, "WATCHLIST", tmp_path / "journal" / "watchlist.json")
    store.ENTRIES.mkdir(parents=True)
    reporting.REPORTS.mkdir()
    return tmp_path


@pytest.fixture
def client(home):
    # A loopback host, as a browser on the operator's machine sends it
    # (TestClient's default, "testserver", is refused like any other name).
    return TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)


@functools.cache
def _ko_ledger_json() -> str:
    """A real ledger: the KO fixture mapped, analysed and ledgered as a
    report run does it."""
    from app.core.pipeline import analyze
    from app.services.ingestion.companyfacts_mapper import build_dataset
    from app.services.reporting.ledger import build_ledger

    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    return build_ledger(result=analyze(ds), dataset=ds, ticker="KO",
                        report_date=date(2026, 9, 29)).model_dump_json()


def _ko_ledger(*, cik_url: bool = True, extra: tuple = (), unsourced: tuple = ()) -> LedgerDocument:
    """The real KO ledger, with an offering row whose document URL names the
    company's CIK (what lets accessions link to EDGAR), a row derived from
    other rows, and whatever else a test adds."""
    doc = LedgerDocument.model_validate_json(_ko_ledger_json())
    items = list(doc.items)
    if cik_url:
        items.append(EvidenceItem(
            id="EV-0ff0000001", plane=Plane.CAPITAL_MARKETS, kind="offering", subject="424B5",
            claim="424B5 filed 2026-08-01 (takedown, common)",
            provenance=(Provenance(accession="0000021344-26-000010", form="424B5",
                                   filed=date(2026, 8, 1), role="takedown", url=KO_DOC),),
            validation_status="directional"))
    items.append(EvidenceItem(
        id="EV-de00000001", plane=Plane.CONSISTENCY, kind="mismatch", subject="demand_vs_wc",
        claim="narrative up, metrics down", derived_from=(items[0].id,),
        validation_status="unvalidated"))
    items.extend(extra)
    return doc.model_copy(update=dict(items=items, unsourced=[*doc.unsourced, *unsourced]))


def _sourced(doc: LedgerDocument) -> list[EvidenceItem]:
    return [i for i in doc.items if i.provenance]


def _publish(ticker: str, day: str, doc: LedgerDocument, text: str | None = None) -> str:
    """Publish one report run (report + ledger) as a rebuild does; its id."""
    out = reporting.report_path(ticker, day)
    with recording() as made, replacing(out) as staged:
        staged.report.write_text(text or f"# {ticker} report\n\n| Block | Score |\n|---|---|\n| EQ | 29 |\n")
        staged.ledger.write_text(doc.model_dump_json())
    return made[-1].generation_id


def _entry(ticker: str = "KO") -> tuple[Path, str]:
    path = store.open_entry(ticker, "a thesis", 3, "hold")
    return path, path.stem.split("_", 1)[1]


def _review_file(ticker: str, day: str) -> Path:
    return store.ENTRIES.parent / "reviews" / f"{ticker}_{day}.review.json"


def _tick(client, ticker, day, gid, key, state="reconciled", note=""):
    return client.post(f"/review/{ticker}/reconcile", data={
        "date": day, "generation": gid, "key": key, "state": state, "note": note})


def _row(html: str, key: str) -> str:
    """The case page's table row for ledger row ``key``."""
    m = re.search(rf'<tr id="{key}".*?</tr>', html, re.S)
    assert m, f"no row {key}"
    return m.group(0)


# --- the case page -------------------------------------------------------------------


def test_the_case_shows_every_sourced_row_with_its_filings_and_the_unsourced_apart(client):
    _, day = _entry()
    doc = _ko_ledger(unsourced=(Unsourced(
        plane=Plane.NARRATIVE, kind="narrative_evidence", subject="guidance_tone",
        claim="guidance softened", reason="no single document identified"),))
    gid = _publish("KO", day, doc)
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200
    assert "KO report" in r.text and "<table>" in r.text          # the report, rendered
    assert gid in r.text
    sourced = _sourced(doc)
    assert len(sourced) > 20                                       # a real ledger's rows
    for item in sourced:
        row = _row(r.text, item.id)
        assert item.subject in row
        for p in item.provenance:
            assert p.accession in row and p.form in row and str(p.filed) in row
            # The folder of the filing on EDGAR, from the CIK the ledger carries.
            assert (f'href="https://www.sec.gov/Archives/edgar/data/{KO_CIK}/'
                    f'{p.accession.replace("-", "")}/"') in row
    # Rows derived from other rows are not reconciled themselves...
    assert '<tr id="EV-de00000001"' not in r.text and "EV-de00000001" in r.text
    # ...and a claim with no source is listed apart, as nothing to check against.
    unsourced = r.text.split('id="unsourced"', 1)[1]
    assert "no document to check against" in unsourced
    assert "guidance softened" in unsourced and "no single document identified" in unsourced
    assert f"0 of {len(sourced)} rows reconciled" in r.text


def test_a_revised_input_is_said_on_its_row(client):
    _, day = _entry()
    base = _sourced(_ko_ledger())[0]
    revised = base.model_copy(update=dict(
        id="EV-ae00000001", change_state="reads_revised_input",
        note="reads revised input(s): revenue FY2025Q4 was 11,000,000,000"))
    gid = _publish("KO", day, _ko_ledger(extra=(revised,)))
    row = _row(client.get(f"/review/KO?date={day}").text, "EV-ae00000001")
    assert "reads_revised_input" in row and "reads revised input(s)" in row
    assert gid


def test_without_a_cik_in_the_ledger_accessions_are_text_not_guessed_links(client):
    _, day = _entry()
    doc = _ko_ledger(cik_url=False)
    _publish("KO", day, doc)
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200
    assert "sec.gov/Archives" not in r.text
    first = _sourced(doc)[0]
    assert first.provenance[0].accession in _row(r.text, first.id)
    assert "CIK" in r.text                                        # and says why


def test_ledger_text_is_escaped_never_markup(client):
    _, day = _entry()
    evil = _sourced(_ko_ledger())[0].model_copy(update=dict(
        id="EV-e0e0e0e0e0", claim=f"claim {XSS}", note=f"note {XSS}", subject=f"s{XSS}"))
    doc = _ko_ledger(extra=(evil,), unsourced=(Unsourced(
        plane=Plane.NARRATIVE, kind="k", subject="s", claim=XSS, reason=XSS),))
    gid = _publish("KO", day, doc, text=f"# KO\n\n{XSS}\n")
    assert _tick(client, "KO", day, gid, "EV-e0e0e0e0e0", "disputed", XSS).status_code == 303
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200
    assert "<script>alert" not in r.text           # the page's own script tag aside
    assert "&lt;script&gt;alert" in r.text
    board = client.get("/review").text
    assert "<script>alert" not in board


def test_a_ledger_of_another_run_is_said_and_never_reconciled_against(client):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    gen = current_generation(reporting.report_path("KO", day))
    ledger = gen / f"KO_{day}.ledger.json"
    other = "f" * 32
    ledger.unlink()                  # a hand copy of another run's ledger over this one's
    ledger.write_text(doc.model_copy(update=dict(generation_id=other)).model_dump_json())
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200
    assert other in r.text and gid in r.text and "not this run" in r.text
    assert '<tr id="EV-' not in r.text                            # no table of another run's facts
    key = _sourced(doc)[0].id
    refused = _tick(client, "KO", day, gid, key)
    assert refused.status_code == 409 and not _review_file("KO", day).exists()
    assert client.get(f"/review/KO/export?date={day}").status_code == 409


def test_a_stale_audit_is_said_and_a_matching_one_shown(client):
    _, day = _entry()
    gid = _publish("KO", day, _ko_ledger())
    gen = current_generation(reporting.report_path("KO", day))
    audit = gen / f"KO_{day}_audit.md"
    audit.write_text(f"<!-- generation: {gid} -->\n\n# KO audit\n\nall clear\n")
    r = client.get(f"/review/KO?date={day}")
    assert "audit of this run" in r.text and "all clear" in r.text
    export = f"/review/KO/export?date={day}&format=md"
    assert "- Audit: present, of this run" in client.get(export).text
    audit.unlink()
    audit.write_text(f"<!-- generation: {'a' * 32} -->\n\n# KO audit\n\nold news\n")
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200
    assert "stale" in r.text and "a" * 32 in r.text
    assert "old news" not in r.text                               # never shown as this run's
    assert f"- Audit: stale: it names run {'a' * 32}, not this one" in client.get(export).text
    audit.unlink()
    assert "- Audit: none yet" in client.get(export).text


def test_a_run_from_before_generations_is_shown_but_cannot_be_ticked(client):
    _, day = _entry()
    doc = _ko_ledger()
    report = reporting.report_path("KO", day)
    report.write_text("# KO plain report\n")
    report.with_name(f"KO_{day}.ledger.json").write_text(doc.model_dump_json())
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 200 and "KO plain report" in r.text
    assert "no generation id" in r.text
    assert 'name="state"' not in r.text                           # no tick forms
    key = _sourced(doc)[0].id
    assert _tick(client, "KO", day, "0" * 32, key).status_code == 409
    assert not _review_file("KO", day).exists()


def test_a_case_with_no_report_is_404(client):
    _, day = _entry()
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 404 and "no live report" in r.text
    r = _tick(client, "KO", day, "0" * 32, "EV-0000000000")
    assert r.status_code == 404 and "no live report" in r.text
    assert not _review_file("KO", day).exists()


# --- ticks ---------------------------------------------------------------------------


def test_a_tick_round_trips(client):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[1].id
    assert "Ticks cannot be recorded" not in client.get(f"/review/KO?date={day}").text
    assert _tick(client, "KO", day, gid, key, "reconciled", "x" * 2000).status_code == 303
    r = _tick(client, "KO", day, gid, key, "reconciled", "matches the 10-Q, p.4")
    assert r.status_code == 303
    assert r.headers["location"] == f"/review/KO?date={day}#{key}"
    page = client.get(r.headers["location"]).text
    row = _row(page, key)
    assert "reconciled" in row and "matches the 10-Q, p.4" in row
    assert f"1 of {len(_sourced(doc))} rows reconciled" in page
    stored = json.loads(_review_file("KO", day).read_text())
    tick = stored["runs"][gid][key]
    assert (tick["state"], tick["note"]) == ("reconciled", "matches the 10-Q, p.4")
    assert stored["ticker"] == "KO" and stored["day"] == day
    # A second tick on the row replaces the first; other rows are untouched.
    other = _sourced(doc)[2].id
    assert _tick(client, "KO", day, gid, other, "disputed", "value differs").status_code == 303
    assert _tick(client, "KO", day, gid, key, "unchecked").status_code == 303
    runs = json.loads(_review_file("KO", day).read_text())["runs"][gid]
    assert key not in runs and runs[other]["state"] == "disputed"   # a reset is no tick
    assert f"0 of {len(_sourced(doc))} rows reconciled" in client.get(f"/review/KO?date={day}").text


def test_ticks_bind_to_the_run_they_were_made_on(client):
    """Ledger ids are content-derived, so a rebuild on the same facts has the
    SAME row keys. A tick keyed by row alone would read as a check of the new
    run, which nobody made."""
    _, day = _entry()
    doc = _ko_ledger()
    first = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    assert _tick(client, "KO", day, first, key, "reconciled", "ok").status_code == 303
    page = client.get(f"/review/KO?date={day}").text
    assert "earlier run" not in page and "Ticks cannot be recorded" not in page
    assert _tick(client, "KO", day, first, _sourced(doc)[1].id, "disputed").status_code == 303
    second = _publish("KO", day, doc)                              # a rebuild: a new run
    page = client.get(f"/review/KO?date={day}").text
    assert second in page
    row = _row(page, key)
    assert "reconciled" not in row.split("<select", 1)[0] and ">unchecked<" in row
    assert f"2 ticks recorded for an earlier run {first}" in page
    md = client.get(f"/review/KO/export?date={day}&format=md").text
    assert f"- 2 ticks recorded for an earlier run {first} (not this run's)" in md
    assert f"generation {second}" in md
    assert f"0 of {len(_sourced(doc))} rows reconciled" in page
    assert f"0 / {len(_sourced(doc))}" in _board_row(client.get("/review").text, "KO")
    # A tick for the run that is no longer live is refused, and not written.
    before = _review_file("KO", day).read_bytes()
    r = _tick(client, "KO", day, first, key, "disputed")
    assert r.status_code == 409 and first in r.text and second in r.text
    assert _review_file("KO", day).read_bytes() == before
    # So is one for a run that never was.
    assert _tick(client, "KO", day, "9" * 32, key).status_code == 409
    assert _review_file("KO", day).read_bytes() == before
    # Put the first run back: its tick applies to it again.
    restore(reporting.report_path("KO", day), first)
    row = _row(client.get(f"/review/KO?date={day}").text, key)
    assert ">reconciled<" in row.split("<select", 1)[0]


@pytest.mark.parametrize("ticker,field,value,status", [
    ("K$O", None, None, 400),
    ("TOOLONGTICKER1", None, None, 400),
    ("KO", "date", "2026-9-1", 400),
    ("KO", "date", "2026-13-45", 400),
    ("KO", "date", "../../etc", 400),
    ("KO", "generation", "G" * 32, 400),
    ("KO", "generation", "0" * 31, 400),
    ("KO", "key", "EV-../../x", 400),
    ("KO", "key", "EV-0123456789", 404),                         # well-formed, not in the ledger
    ("KO", "key", "EV-de00000001", 404),                         # derived: nothing to check
    ("KO", "state", "approved", 400),
    ("KO", "note", "x" * 2001, 400),
    ("KO", "note", "a\x00b", 400),
], ids=lambda v: str(v)[:12])
def test_bad_input_is_refused_and_nothing_is_written(client, ticker, field, value, status):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    data = {"date": day, "generation": gid, "key": _sourced(doc)[0].id,
            "state": "reconciled", "note": ""}
    if field:
        data[field] = value
    r = client.post(f"/review/{ticker}/reconcile", data=data)
    assert r.status_code == status
    assert "Tick not recorded" in r.text          # said, not a bare framework error
    if field == "key" and status == 404:
        assert value in r.text
    reviews = store.ENTRIES.parent / "reviews"
    assert not reviews.exists() or not any(reviews.iterdir())


def test_a_tick_posted_from_another_site_is_refused(client):
    """The console listens on loopback; any page the reviewer has open can
    post its form. A forged "reconciled" must not land."""
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    data = {"date": day, "generation": gid, "key": key, "state": "reconciled", "note": ""}
    for origin in ("https://evil.example", "null"):
        r = client.post("/review/KO/reconcile", data=data, headers={"Origin": origin})
        assert r.status_code == 403 and "another site" in r.text
    assert not _review_file("KO", day).exists()
    same = client.post("/review/KO/reconcile", data=data, headers={"Origin": "http://127.0.0.1"})
    assert same.status_code == 303 and _review_file("KO", day).exists()


def test_bad_case_names_are_refused_on_every_route(client):
    for url in ("/review/K$O?date=2026-09-29", "/review/KO?date=2026-9-29",
                "/review/KO/export?date=../x", "/review/KO/export?date=2026-09-29&format=exe"):
        assert client.get(url).status_code == 400, url


def test_a_symlink_at_the_review_file_is_not_written_through(client, tmp_path):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    outside = tmp_path / "precious.json"
    outside.write_text("precious\n")
    path = _review_file("KO", day)
    path.parent.mkdir()
    path.symlink_to(outside)
    r = _tick(client, "KO", day, gid, _sourced(doc)[0].id)
    assert r.status_code == 409 and "not a regular file" in r.text
    assert outside.read_text() == "precious\n" and path.is_symlink()
    page = client.get(f"/review/KO?date={day}")
    assert page.status_code == 200 and "not a regular file" in page.text


def test_a_symlink_at_the_reviews_folder_is_not_written_through(client, tmp_path):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (store.ENTRIES.parent / "reviews").symlink_to(elsewhere)
    r = _tick(client, "KO", day, gid, _sourced(doc)[0].id)
    assert r.status_code == 409 and "symlink" in r.text
    assert list(elsewhere.iterdir()) == []


def test_a_symlink_at_the_lock_is_not_followed(client, tmp_path):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    reviews = store.ENTRIES.parent / "reviews"
    reviews.mkdir()
    target = tmp_path / "created-through-the-lock"
    (reviews / f".KO_{day}.review.json.lock").symlink_to(target)
    r = _tick(client, "KO", day, gid, _sourced(doc)[0].id)
    assert r.status_code == 409
    assert not target.exists() and not _review_file("KO", day).exists()


def test_an_unreadable_review_file_is_said_and_never_overwritten(client):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    path = _review_file("KO", day)
    path.parent.mkdir()
    path.write_text("{torn")
    r = _tick(client, "KO", day, gid, _sourced(doc)[0].id)
    assert r.status_code == 409 and path.read_text() == "{torn"
    page = client.get(f"/review/KO?date={day}")
    assert page.status_code == 200 and "cannot be read" in page.text
    assert "Ticks cannot be recorded" in page.text and 'name="state"' not in page.text
    # One naming another case is not this case's either.
    from app.services.journal import review

    path.write_text(json.dumps({"format": review.FORMAT, "ticker": "PEP", "day": day,
                                "runs": {}}))
    assert _tick(client, "KO", day, gid, _sourced(doc)[0].id).status_code == 409


def _ticker_in_process(args):
    """One reviewer's tick, in a process of its own: the load that read the
    file is made slow, so two ticks without the lock would both read the
    file before either wrote it, and one would be lost."""
    from app.services.journal import review

    entries, reports, gid, key, day = args
    store.ENTRIES = Path(entries)
    reporting.REPORTS = Path(reports)
    real = review._load

    def slow(*a, **kw):
        doc = real(*a, **kw)
        time.sleep(0.4)
        return doc

    review._load = slow
    review.record_tick("KO", day, gid, key, "reconciled", f"by {os.getpid()}")
    return key


def test_two_reviewers_ticking_at_once_both_land(home):
    import multiprocessing as mp

    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    keys = [i.id for i in _sourced(doc)[:2]]
    ctx = mp.get_context("fork")
    with ctx.Pool(2) as pool:
        done = pool.map(_ticker_in_process,
                        [(str(store.ENTRIES), str(reporting.REPORTS), gid, k, day) for k in keys])
    assert sorted(done) == sorted(keys)
    runs = json.loads(_review_file("KO", day).read_text())["runs"][gid]
    assert sorted(runs) == sorted(keys)
    assert not [p for p in _review_file("KO", day).parent.iterdir() if p.name.endswith(".tmp")]


# --- the export ----------------------------------------------------------------------


def test_the_export_is_the_live_runs_reconciliation(client):
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    _tick(client, "KO", day, gid, key, "disputed", "=HYPERLINK(\"http://x\")")
    r = client.get(f"/review/KO/export?date={day}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f"KO_{day}_review.csv" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == len(_sourced(doc))
    mine = next(x for x in rows if x["key"] == key)
    assert mine["state"] == "disputed" and mine["generation_id"] == gid
    assert mine["note"].startswith("'=")                          # not a formula in a spreadsheet
    assert _sourced(doc)[0].provenance[0].accession in mine["accessions"]
    md = client.get(f"/review/KO/export?date={day}&format=md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert gid in md.text and key in md.text and "disputed" in md.text
    assert "0 of" in md.text and "engine" in md.text.lower()


# --- the board -----------------------------------------------------------------------


def _watch(rows: list[dict]) -> None:
    wl.WATCHLIST.write_text(json.dumps({"watchlist": rows}))


def _board_row(html: str, ticker: str) -> str:
    m = re.search(rf'<tr data-case="{ticker}_.*?</tr>', html, re.S)
    assert m, f"no board row for {ticker}"
    return m.group(0)


def test_the_board_shows_each_cases_state(client):
    ko, day = _entry("KO")
    pep, _ = _entry("PEP")
    mmm, _ = _entry("MMM")
    ibm, _ = _entry("IBM")
    _entry("NVDA")
    _watch([
        {"ticker": "KO", "print_at": "2026-09-30T20:00:00Z", "thesis_entry": day,
         "thesis_sha256": "0" * 64},
        {"ticker": "NVDA", "print_at": "2026-09-30T20:20:00Z", "label": "FQ3-27",
         "baseline_accession": "", "expected_report_date": "2026-07-31",
         "thesis_entry": day, "thesis_sha256": "0" * 64},
        {"ticker": "AMD", "print_at": "2026-10-01T20:20:00Z"},
    ])
    doc = _ko_ledger()
    live = _publish("KO", day, doc)                                 # live, nothing else
    pending = _publish("PEP", day, doc)
    store.set_report_pending(pep, "journal.py report --defer-mark",
                             store.ReportOwner("watch.py sweep", 1, "elsewhere", "t"),
                             generation_id=pending, report="r")
    unstamped = _publish("MMM", day, doc)
    assert store.mark_not_stamped(mmm, "the web report page",
                                  store.ReportOwner.this_process("the web report page"),
                                  unstamped, "r") == (True, None)
    stamped = _publish("IBM", day, doc)
    store.mark_reported(ibm)
    gen = current_generation(reporting.report_path("IBM", day))
    (gen / f"IBM_{day}_audit.md").write_text(f"<!-- generation: {'b' * 32} -->\n# audit\n")
    r = client.get("/review")
    assert r.status_code == 200
    total = len(_sourced(doc))
    nvda = _board_row(r.text, "NVDA")
    assert "no report yet" in nvda and "FQ3-27" in nvda
    assert "no journal entry pinned" in _board_row(r.text, "AMD")
    assert r.text.count(f'data-case="KO_{day}"') == 1          # watched and journaled: one case
    row = _board_row(r.text, "KO")
    assert "report live" in row and live in row and f"0 / {total}" in row
    built = current_generation(reporting.report_path("KO", day)).name.split("_")[0]
    assert (f"built {built[:4]}-{built[4:6]}-{built[6:8]} {built[9:11]}:{built[11:13]}:"
            f"{built[13:15]} UTC") in row
    assert "entry not stamped" in row and "published, not stamped" not in row
    assert "pending" not in row
    assert f'href="/review/KO?date={day}"' in row
    row = _board_row(r.text, "PEP")
    assert "pending its audit" in row and pending in row
    row = _board_row(r.text, "MMM")
    assert "published, not stamped" in row and unstamped in row
    row = _board_row(r.text, "IBM")
    assert "stamped reported" in row and stamped in row and "stale audit" in row
    # A tick shows in the count; the matching audit is said as such.
    _tick(client, "KO", day, live, _sourced(doc)[0].id)
    (current_generation(reporting.report_path("KO", day)) / f"KO_{day}_audit.md").write_text(
        f"<!-- generation: {live} -->\n# audit\n")
    row = _board_row(client.get("/review").text, "KO")
    assert f"1 / {total}" in row and "audit matches" in row


def test_the_board_survives_a_broken_watchlist(client):
    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())
    wl.WATCHLIST.write_text("{not json")
    r = client.get("/review")
    assert r.status_code == 200 and "watchlist" in r.text.lower() and "invalid JSON" in r.text
    assert "report live" in _board_row(r.text, "KO")


def test_the_board_is_linked_from_the_existing_pages(client):
    assert 'href="/review"' in client.get("/").text
    assert 'href="/review"' in client.get("/open").text


def test_the_board_writes_nothing(client, home):
    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())
    before = sorted(str(p) for p in home.rglob("*"))
    for url in ("/review", f"/review/KO?date={day}", f"/review/KO/export?date={day}"):
        assert client.get(url).status_code == 200, url
    assert sorted(str(p) for p in home.rglob("*")) == before
    assert report_files.read_live(reporting.report_path("KO", day)) is not None


def test_values_read_as_the_filing_prints_them():
    from app.services.journal import review

    assert review.fmt_value(None) == "—"
    assert review.fmt_value(1000.0) == "1,000"             # a filing's figure, grouped
    assert review.fmt_value(-2500.0) == "-2,500"
    assert review.fmt_value(999.5) == "999.5"              # a ratio, to four figures
    assert review.fmt_value(1.1439689) == "1.144"


def test_the_newest_entry_is_the_case_when_no_day_is_named(client):
    _, day = _entry()
    _publish("KO", day, _ko_ledger())
    r = client.get("/review/KO")
    assert r.status_code == 200 and f"KO {day}" in r.text
    r = client.get("/review/PEP")
    assert r.status_code == 404 and "has no journal entry" in r.text


def test_engine_is_the_ledgers_then_the_reports(client):
    """Files from before generations: the ledger's engine line wins, the
    report's is read when the ledger has none."""
    _, day = _entry()
    report = reporting.report_path("KO", day)
    report.write_text("# KO\n\n- Engine: from-the-report\n")
    ledger = report.with_name(f"KO_{day}.ledger.json")
    ledger.write_text(_ko_ledger().model_copy(update=dict(engine_commit="from-the-ledger"))
                      .model_dump_json())
    assert "Engine: from-the-ledger" in client.get(f"/review/KO?date={day}").text
    ledger.write_text(_ko_ledger().model_dump_json())
    assert "Engine: from-the-report" in client.get(f"/review/KO?date={day}").text


def test_two_ciks_link_nothing_and_say_why(client):
    _, day = _entry()
    other = EvidenceItem(
        id="EV-0ff0000002", plane=Plane.CAPITAL_MARKETS, kind="offering", subject="S-3",
        claim="S-3 filed 2026-05-02 (shelf)",
        provenance=(Provenance(accession="0000021344-26-000011", form="S-3",
                               filed=date(2026, 5, 2), role="shelf",
                               url="https://www.sec.gov/Archives/edgar/data/99/x/s3.htm"),),
        validation_status="directional")
    _publish("KO", day, _ko_ledger(extra=(other,)))
    r = client.get(f"/review/KO?date={day}")
    assert "more than one CIK (99, 21344)" in r.text and "sec.gov/Archives" not in r.text


def test_a_subtracted_source_is_said_beside_its_value_as_filed(client):
    _, day = _entry()
    item = EvidenceItem(
        id="EV-5b00000001", plane=Plane.ACCOUNTING, kind="metric", subject="cfo",
        claim="cfo for Q2", validation_status="directional", provenance=(
            Provenance(accession="0000021344-26-000001", form="10-Q", filed=date(2026, 7, 1),
                       value=900.0, sign=1, method="ytd_diff", role="cfo"),
            Provenance(accession="0000021344-26-000002", form="10-Q", filed=date(2026, 4, 1),
                       value=400.0, sign=-1, method="ytd_diff", role="cfo")))
    _publish("KO", day, _ko_ledger(extra=(item,)))
    items = re.findall(r"<li>.*?</li>", _row(client.get(f"/review/KO?date={day}").text,
                                             "EV-5b00000001"), re.S)
    assert len(items) == 2
    assert "900" in items[0] and "subtracted" not in items[0]
    assert "400 · subtracted" in items[1] and "-400" not in items[1]


def _dir_fsyncs_fail(monkeypatch):
    import errno
    import stat

    real = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def test_a_tick_whose_folder_fsync_fails_is_said_as_recorded_not_lost(client, monkeypatch):
    """The write renames the file into place, then fsyncs its folder. A
    failed fsync raises with the tick there: said as recorded, durability
    unconfirmed, never as "not recorded"."""
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    _dir_fsyncs_fail(monkeypatch)
    r = _tick(client, "KO", day, gid, key, "reconciled", "ok")
    assert r.status_code == 500 and "The tick is recorded" in r.text
    assert json.loads(_review_file("KO", day).read_text())["runs"][gid][key]["note"] == "ok"


def test_a_tick_whose_write_fails_is_said_as_not_recorded(client, monkeypatch):
    import errno

    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    assert _tick(client, "KO", day, gid, key, "reconciled", "first").status_code == 303

    def full(*a, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store, "_durable_write", full)
    r = _tick(client, "KO", day, gid, key, "disputed", "second")
    assert r.status_code == 500 and "could not be recorded" in r.text
    assert "No space left on device" in r.text
    assert json.loads(_review_file("KO", day).read_text())["runs"][gid][key]["note"] == "first"



# --- review of 2f26846 -----------------------------------------------------------------


def _rebinding_setup():
    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    return day, {"date": day, "generation": gid, "key": _sourced(doc)[0].id,
                 "state": "reconciled", "note": ""}


def test_a_rebound_host_is_refused_on_every_route(client, monkeypatch):
    """Finding 1: DNS rebinding. A page on rebind.evil.example, re-resolved
    to 127.0.0.1, is same-origin with itself: its Host and Origin agree, so
    comparing the two let its forged tick land (after it read the run and
    the row ids with a GET). Only a loopback name is served."""
    monkeypatch.delenv("FQE_WEB_ALLOWED_HOSTS", raising=False)
    day, data = _rebinding_setup()
    evil = {"host": "rebind.evil.example:8000"}
    for url in ("/", "/review", f"/review/KO?date={day}", f"/review/KO/export?date={day}"):
        assert client.get(url, headers=evil).status_code == 400, url
    r = client.post("/review/KO/reconcile", data=data,
                    headers={**evil, "origin": "http://rebind.evil.example:8000"})
    assert r.status_code == 400 and not _review_file("KO", day).exists()
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "127.0.0.1", "LOCALHOST"):
        assert client.get("/review", headers={"host": host}).status_code == 200, host
    for host in ("", "[::1", "127.0.0.1.evil.example", "evil.example#@127.0.0.1",
                 "127.0.0.1:99999", "127.0.0.1:x", "user@127.0.0.1", "127.0.0.1/x"):
        assert client.get("/review", headers={"host": host}).status_code == 400, host


def test_the_served_names_can_be_set_for_a_deployment(client, monkeypatch):
    monkeypatch.setenv("FQE_WEB_ALLOWED_HOSTS", " journal.lan , Review.Box ")
    assert client.get("/review", headers={"host": "journal.lan:8000"}).status_code == 200
    assert client.get("/review", headers={"host": "review.box"}).status_code == 200
    assert client.get("/review", headers={"host": "127.0.0.1:8000"}).status_code == 400


@pytest.mark.parametrize("site,status", [
    ("cross-site", 403), ("same-site", 403), ("same-origin", 303), ("none", 303)])
def test_a_tick_from_a_page_that_is_not_the_consoles_is_refused(client, site, status):
    day, data = _rebinding_setup()
    r = client.post("/review/KO/reconcile", data=data, headers={"sec-fetch-site": site})
    assert r.status_code == status
    assert _review_file("KO", day).exists() == (status == 303)


def test_origin_must_be_the_consoles_own_and_a_missing_one_is_a_non_browser(client):
    """A browser sends Origin on every form POST; a POST without one comes
    from a script (curl), which is no forgery vector, and is accepted."""
    day, data = _rebinding_setup()
    for origin in ("http://127.0.0.1:9000", "https://127.0.0.1", "http://localhost"):
        r = client.post("/review/KO/reconcile", data=data, headers={"origin": origin})
        assert r.status_code == 403, origin
    assert not _review_file("KO", day).exists()
    assert client.post("/review/KO/reconcile", data=data).status_code == 303


def _latin1_audit(day, gid):
    gen = current_generation(reporting.report_path("KO", day))
    (gen / f"KO_{day}_audit.md").write_bytes(
        f"<!-- generation: {gid} -->\n# audit \xe9\n".encode("latin-1"))


def test_one_case_that_cannot_be_decoded_does_not_take_the_board_down(client):
    """Finding 2: a non-UTF-8 audit raised UnicodeDecodeError (a ValueError,
    not an OSError) out of read_live and 500ed the whole board."""
    _, day = _entry("KO")
    gid = _publish("KO", day, _ko_ledger())
    _entry("PEP")
    _publish("PEP", day, _ko_ledger())
    _latin1_audit(day, gid)
    r = client.get("/review")
    assert r.status_code == 200
    row = _board_row(r.text, "KO")
    assert "report unreadable" in row and "cannot be read" in row
    assert "report live" in _board_row(r.text, "PEP")
    page = client.get(f"/review/KO?date={day}")
    assert page.status_code == 500 and "cannot be read" in page.text and "Case not shown" in page.text
    assert client.get(f"/review/PEP?date={day}").status_code == 200
    tick = _tick(client, "KO", day, gid, _sourced(_ko_ledger())[0].id)
    assert tick.status_code == 500 and not _review_file("KO", day).exists()


def test_a_report_that_cannot_be_decoded_is_a_clean_error(client):
    _, day = _entry("KO")
    report = reporting.report_path("KO", day)
    report.write_bytes(b"# KO \xff report\n")
    report.with_name(f"KO_{day}.ledger.json").write_text(_ko_ledger().model_dump_json())
    assert client.get("/review").status_code == 200
    r = client.get(f"/review/KO?date={day}")
    assert r.status_code == 500 and "cannot be read" in r.text


def test_a_watchlist_that_cannot_be_decoded_is_said_on_the_board(client):
    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())
    wl.WATCHLIST.write_bytes(b'{"watchlist": [], "x": "\xff"}')
    r = client.get("/review")
    assert r.status_code == 200 and "The watchlist cannot be read" in r.text
    assert "report live" in _board_row(r.text, "KO")


def test_any_failure_of_one_case_is_that_rows_problem(client, monkeypatch):
    from app.services.journal import review

    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())
    _entry("PEP")
    _publish("PEP", day, _ko_ledger())
    real = review.case

    def case(ticker, d):
        if ticker == "KO":
            raise RuntimeError("something nobody foresaw")
        return real(ticker, d)

    monkeypatch.setattr(review, "case", case)
    r = client.get("/review")
    assert r.status_code == 200
    row = _board_row(r.text, "KO")
    assert "RuntimeError: something nobody foresaw" in row and "report unreadable" in row
    assert "report live" in _board_row(r.text, "PEP")


def test_a_rebuild_between_the_check_and_the_write_is_refused(client, monkeypatch):
    """Finding 3: the run was checked before the lock; a rebuild landing
    in between stored the tick under the run no longer live, answered 303."""
    import contextlib

    from app.services.journal import review

    _, day = _entry()
    doc = _ko_ledger()
    first = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    real, made = report_files.publish_lock, []

    @contextlib.contextmanager
    def racing(report):
        # The rebuild commits just before the tick takes the publish lock
        # (a publish inside it would wait for the tick instead). The
        # rebuild's own publish takes the real lock.
        if not made:
            made.append("building")
            made[0] = _publish("KO", day, doc)
        with real(report):
            yield

    monkeypatch.setattr(report_files, "publish_lock", racing)
    r = _tick(client, "KO", day, first, key)
    assert r.status_code == 409 and first in r.text and made[0] in r.text
    runs = review._load(_review_file("KO", day), "KO", day)
    assert first not in runs


def test_a_reset_to_unchecked_leaves_no_tick(client):
    """Finding 4: a reset was stored as a tick, and after a rebuild read as
    "1 tick recorded for an earlier run" of a run nobody reconciled."""
    _, day = _entry()
    doc = _ko_ledger()
    first = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    other = _sourced(doc)[1].id
    assert _tick(client, "KO", day, first, key, "reconciled").status_code == 303
    assert _tick(client, "KO", day, first, other, "disputed").status_code == 303
    assert _tick(client, "KO", day, first, key, "unchecked", "looked again").status_code == 303
    runs = json.loads(_review_file("KO", day).read_text())["runs"]
    assert list(runs[first]) == [other]
    assert _tick(client, "KO", day, first, other, "unchecked").status_code == 303
    assert json.loads(_review_file("KO", day).read_text())["runs"] == {}
    _publish("KO", day, doc)
    assert "earlier run" not in client.get(f"/review/KO?date={day}").text
    assert "earlier run" not in client.get(f"/review/KO/export?date={day}&format=md").text
    assert "earlier runs" not in _board_row(client.get("/review").text, "KO")


def test_an_unchecked_tick_left_by_an_earlier_version_is_not_counted(client):
    _, day = _entry()
    doc = _ko_ledger()
    _publish("KO", day, doc)
    path = _review_file("KO", day)
    path.parent.mkdir()
    keys = [i.id for i in _sourced(doc)[:2]]
    path.write_text(json.dumps({"format": "fqe-review/1", "ticker": "KO", "day": day, "runs": {
        "e" * 32: {keys[0]: {"state": "unchecked", "note": "", "at": "2026-09-30T00:00:00Z"},
                   keys[1]: {"state": "reconciled", "note": "", "at": "2026-09-30T00:00:00Z"}},
        "f" * 32: {keys[0]: {"state": "unchecked", "note": "", "at": "2026-09-30T00:00:00Z"}}}}))
    page = client.get(f"/review/KO?date={day}").text
    assert f"1 tick recorded for an earlier run {'e' * 32}" in page
    assert "f" * 32 not in page



# --- review of efb8500 -----------------------------------------------------------------


def test_a_publish_during_the_write_waits_for_the_tick(client, monkeypatch):
    """M1: the in-lock re-check took only the review file's lock, which no
    publisher takes: a rebuild committing between the re-check and the
    write left the tick on a run no longer live, said as recorded. The
    tick now holds the case's publish lock across both, so a publish that
    comes then waits (only its commit: the build runs before the lock), and
    the tick lands on the run that was live when it was written."""
    import threading

    from app.services.journal import review

    _, day = _entry()
    doc = _ko_ledger()
    first = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    real, made, during = store._durable_write, [], []

    def racing(path, text, **kw):
        if path.name.endswith(".review.json") and not during:
            rebuild = threading.Thread(target=lambda: made.append(_publish("KO", day, doc)))
            rebuild.start()
            rebuild.join(timeout=1.0)          # a publish free to commit does so at once
            during.append((list(made), rebuild))
        return real(path, text, **kw)

    monkeypatch.setattr(store, "_durable_write", racing)
    r = _tick(client, "KO", day, first, key)
    landed_during, rebuild = during[0]
    rebuild.join(timeout=30)
    assert landed_during == []                 # the publish waited for the tick
    assert r.status_code == 303
    runs = review._load(_review_file("KO", day), "KO", day)
    assert key in runs[first]                  # on the run live when it was written
    live = report_files.read_live(reporting.report_path("KO", day)).generation_id
    assert made and live == made[0] != first   # and the rebuild then went live


def test_a_reset_of_a_row_with_no_tick_writes_nothing(client, monkeypatch):
    """N1: a reset of an untouched row wrote a review file for a no-op, and
    one whose write failed was said as recorded."""
    import errno

    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    assert _tick(client, "KO", day, gid, key, "unchecked").status_code == 303
    assert not (store.ENTRIES.parent / "reviews").exists()

    def full(path, text, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    assert _tick(client, "KO", day, gid, _sourced(doc)[1].id, "disputed").status_code == 303
    monkeypatch.setattr(store, "_durable_write", full)
    r = _tick(client, "KO", day, gid, key, "unchecked")
    assert r.status_code == 303                # still nothing to reset, still no write


def test_a_reset_whose_write_fails_is_said_as_not_recorded(client, monkeypatch):
    import errno

    _, day = _entry()
    doc = _ko_ledger()
    gid = _publish("KO", day, doc)
    key = _sourced(doc)[0].id
    assert _tick(client, "KO", day, gid, key, "reconciled", "kept").status_code == 303

    def full(path, text, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store, "_durable_write", full)
    r = _tick(client, "KO", day, gid, key, "unchecked")
    assert r.status_code == 500 and "could not be recorded" in r.text
    assert "The tick is recorded" not in r.text
    assert json.loads(_review_file("KO", day).read_text())["runs"][gid][key]["note"] == "kept"


@pytest.mark.parametrize("raw", [
    "[" * 200000, '{"watchlist": [5]}', '{"watchlist": [{"ticker": 5}]}',
    '{"watchlist": [{"ticker": "KO", "print_at": []}]}', '{"watchlist": null}',
    '{"watchlist": [{"ticker": "KO", "print_at": "2026-09-30T20:00:00Z", "forms": 5}]}',
], ids=["deep", "int-row", "int-ticker", "list-print-at", "null", "int-forms"])
def test_a_malformed_watchlist_is_said_on_the_board(client, raw, caplog):
    """L2: a watchlist of the wrong shapes raised out of the loader
    (AttributeError, TypeError, RecursionError) and 500ed the board."""
    import logging

    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())
    wl.WATCHLIST.write_text(raw)
    caplog.set_level(logging.WARNING)
    r = client.get("/review")
    assert r.status_code == 200 and "The watchlist cannot be read" in r.text
    assert "report live" in _board_row(r.text, "KO")
    assert any(x.name == "app.services.journal.review" and x.exc_info for x in caplog.records)


@pytest.mark.parametrize("raw,expected", [
    ({"ticker": 5, "print_at": "2026-09-30T20:00:00Z"}, "invalid ticker"),
    ({"ticker": "KO", "print_at": "2026-09-30T20:00:00Z", "forms": 5}, "forms must be a list"),
])
def test_the_watchlist_parser_refuses_wrong_types_as_a_watchlist_error(raw, expected):
    with pytest.raises(wl.WatchlistError, match=expected):
        wl.parse_watch(raw)


def test_a_ticker_that_is_not_text_is_a_value_error():
    for bad in (5, None, ["KO"]):
        with pytest.raises(ValueError, match="invalid ticker"):
            store.safe_ticker(bad)


def test_a_case_that_fails_on_the_board_is_logged_with_its_traceback(client, monkeypatch, caplog):
    """L3: the row said the error, and nothing else did."""
    import logging

    from app.services.journal import review

    _, day = _entry("KO")
    _publish("KO", day, _ko_ledger())

    def broken(ticker, d):
        raise AttributeError("a bug")

    monkeypatch.setattr(review, "case", broken)
    caplog.set_level(logging.WARNING)
    r = client.get("/review")
    assert r.status_code == 200 and "AttributeError: a bug" in _board_row(r.text, "KO")
    [rec] = [x for x in caplog.records if x.name == "app.services.journal.review"]
    assert rec.exc_info and "KO" in rec.getMessage()


def test_served_names_with_a_port_match_the_name(client, monkeypatch):
    """L4: an entry with a port never matched, so every request was 400."""
    monkeypatch.setenv("FQE_WEB_ALLOWED_HOSTS", "journal.lan:8000,[::1]:8000")
    assert client.get("/review", headers={"host": "journal.lan:8000"}).status_code == 200
    assert client.get("/review", headers={"host": "journal.lan:9000"}).status_code == 200
    assert client.get("/review", headers={"host": "[::1]:8000"}).status_code == 200
    monkeypatch.setenv("FQE_WEB_ALLOWED_HOSTS", "::1")
    assert client.get("/review", headers={"host": "[::1]:8000"}).status_code == 200


def test_a_served_name_that_is_not_a_host_is_said_loudly(client, monkeypatch):
    monkeypatch.setenv("FQE_WEB_ALLOWED_HOSTS", "journal.lan,bad/host")
    r = client.get("/review", headers={"host": "journal.lan"})
    assert r.status_code == 500
    assert "FQE_WEB_ALLOWED_HOSTS" in r.text and "'bad/host'" in r.text


def test_a_cross_site_form_cannot_write_an_entry(client):
    """N2: /impact took a cross-site form (a forged what_happened landed)."""
    path, day = _entry()
    store.mark_reported(path)
    before = path.read_text()
    for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"},
                    {"origin": "http://127.0.0.1", "sec-fetch-site": "same-site"}):
        r = client.post("/impact/KO", data={"date": day, "what_happened": "FORGED"},
                        headers=headers)
        assert r.status_code == 403, headers
    assert path.read_text() == before
    # The app's own form, as a browser posts it, still writes.
    r = client.post("/impact/KO", data={"date": day, "what_happened": "real"},
                    headers={"origin": "http://127.0.0.1", "sec-fetch-site": "same-origin"})
    assert r.status_code == 303 and "what_happened: real" in path.read_text()


def test_a_cross_site_page_cannot_make_the_report_page_build(client, monkeypatch):
    """N2: GET /report builds, publishes and stamps on first view; an <img>
    on another site could trigger it."""
    built = []

    def build(ticker, with_docs=True, report_day=None, fresh=False):
        built.append(ticker)
        p = reporting.report_path(ticker, report_day)
        p.write_text(f"# {ticker} report\n")
        return p, "ok"

    monkeypatch.setattr(reporting, "build_report", build)
    path, _ = _entry()
    for site in ("cross-site", "same-site"):
        r = client.get("/report/KO", headers={"sec-fetch-site": site})
        assert r.status_code == 403
    assert built == [] and not store.parse_entry(path)["is_reported"]
    assert client.get("/report/KO", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert built == ["KO"]
    assert client.get("/report/KO", headers={"sec-fetch-site": "none"}).status_code == 200
