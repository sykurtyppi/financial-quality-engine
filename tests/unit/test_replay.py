"""Historical replay: the report a reader could have had on a past day.

Every report path used to report on today, and said so: a true replay needed
point-in-time fundamentals and a `filed <= as_of` cut on every stream. The
pieces now exist — `build_dataset(as_of=)`, `fetch_documents(before=)`, and
streams that all anchor on `generated_on` — so a replay reads the newest
companyfacts snapshot stored by then (else today's payload cut there), cuts
every stream at the day, archives nothing, and says what it is.

The ledger makes the point-in-time claim checkable: no filing it cites may
postdate the day, and no snapshot it cites may be younger.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.ledger import LedgerDocument
from app.services.ingestion import vintages
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.vintages import read_manifest, store_snapshot
from app.services.journal import reporting as journal_reporting
from app.services.reporting.report_builder import ledger_path
from tests.integration.test_ledger_provenance import (
    AMENDMENT,
    CIK,
    _amended,
    _Client,
    _submissions,
)

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DAY = date(2026, 6, 1)  # after the 4.02 (04-03) and the S-3 (05-02); before the 424B5 and the 10-Q/A
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class _FixedDatetime:
    @staticmethod
    def now(tz=None):
        return NOW


def _payloads():
    base = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(base, "KO")
    return base, _amended(base, ds.sorted_periods()[-2].period_end.isoformat())


def _replay(monkeypatch, tmp_path, live: dict, *, day: date = DAY, with_docs=False):
    seen: dict = {}

    def fake_documents(client, ticker, facts, n_filings=8, submissions=None, before=None):
        seen["before"] = before
        return SimpleNamespace(documents=[], diagnostics=[])

    monkeypatch.setattr(journal_reporting, "SecClient", lambda fresh=False: _Client(live))
    monkeypatch.setattr(journal_reporting, "fetch_submissions_snapshot",
                        lambda ticker, client: _submissions())
    monkeypatch.setattr(journal_reporting, "fetch_documents", fake_documents)
    monkeypatch.setattr(journal_reporting, "datetime", _FixedDatetime)
    out, _ = journal_reporting.build_report(
        "KO", with_docs=with_docs, report_day=day.isoformat(), out_dir=tmp_path, replay=True,
    )
    return out, seen


def _revised_in_place(facts: dict) -> dict:
    """The same payload with the latest total-assets figure changed 10% and
    nothing re-filed — a silent revision between two snapshots."""
    import copy

    out = copy.deepcopy(facts)
    rows = out["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    latest = max(rows, key=lambda r: (r["end"], r["filed"]))
    latest["val"] = latest["val"] * 1.1
    return out


def _store(payload: dict, day: date):
    store_snapshot(CIK, payload, now=datetime(day.year, day.month, day.day, tzinfo=UTC))


def test_a_replay_reads_the_newest_snapshot_stored_by_then(monkeypatch, tmp_path):
    base, amended = _payloads()
    _store(base, date(2026, 5, 1))
    _store(amended, date(2026, 9, 1))  # after the day: invisible
    out, _ = _replay(monkeypatch, tmp_path, live=amended)
    text = out.read_text()
    assert out.name == f"KO_{DAY}.replay.md"
    assert text.startswith(f"> **HISTORICAL REPLAY — as of {DAY}.**")
    assert "the vintage snapshot captured 2026-05-01" in text
    assert AMENDMENT not in text


def test_a_stored_snapshot_is_cut_at_the_day_too():
    """A snapshot captured by the day holds only facts filed by its capture,
    so the cut is a no-op on a sound store. It is still applied: a store
    whose clock ran ahead (here, a payload carrying the 2026-08-15 10-Q/A
    recorded as captured 2026-05-01) must not leak a later filing into the
    mapped values."""
    from app.services.ingestion.edgar_adapter import replay_snapshot

    _base, amended = _payloads()
    _store(amended, date(2026, 5, 1))
    snap, source = replay_snapshot(_Client(amended), "KO", DAY)
    assert "captured 2026-05-01" in source
    cut, _ = build_dataset(amended, "KO", as_of=DAY)
    uncut, _ = build_dataset(amended, "KO")
    assert cut != uncut  # the amendment moves a mapped value
    assert snap.dataset == cut


def test_without_a_stored_snapshot_it_cuts_todays_payload_and_says_so(monkeypatch, tmp_path):
    _base, amended = _payloads()
    out, _ = _replay(monkeypatch, tmp_path, live=amended)
    text = out.read_text()
    assert "today's companyfacts cut to facts filed on or before 2026-06-01" in text
    assert "shows as revised" in text
    assert AMENDMENT not in text  # filed 2026-08-15: cut
    ledger = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    cited = [p for item in ledger.items for p in item.provenance if p.kind == "filing"]
    assert cited and max(p.filed for p in cited) <= DAY
    assert AMENDMENT not in {p.accession for p in cited}


def test_nothing_the_replay_cites_postdates_the_day(monkeypatch, tmp_path):
    base, amended = _payloads()
    _store(base, date(2026, 3, 1))
    _store(_revised_in_place(base), date(2026, 5, 20))
    _store(amended, date(2026, 9, 1))
    out, seen = _replay(monkeypatch, tmp_path, live=amended, with_docs=True)
    assert seen["before"] == DAY
    ledger = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    assert ledger.generated_on == DAY and ledger.items
    for item in ledger.items:
        for p in item.provenance:
            assert (p.filed or p.captured) <= DAY, (item.kind, item.subject, p)
    kinds = {i.kind for i in ledger.items}
    assert {"metric", "offering", "non_reliance_8k_402", "silent_revision"} <= kinds
    offerings = [i.subject for i in ledger.items if i.kind == "offering"]
    assert offerings == ["S-3"]  # the 424B5 of 2026-08-01 did not exist yet
    text = out.read_text()
    assert "0000320193-26-800000" not in text and AMENDMENT not in text


def test_the_replay_is_the_report_that_day_with_a_banner(monkeypatch, tmp_path):
    """Replay identity: the same builder, on the payload it chose, dated the
    day — nothing but the banner added."""
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report

    base, amended = _payloads()
    _store(base, date(2026, 5, 1))
    out, _ = _replay(monkeypatch, tmp_path, live=amended)
    ds, diag = build_dataset(base, "KO", as_of=DAY)
    source = f"the vintage snapshot captured 2026-05-01 (sha {read_manifest(CIK)['snapshots'][0]['sha256'][:12]}), cut to facts filed on or before {DAY}"
    direct, _ = build_report(
        analyze(ds), ds, generated_on=DAY.isoformat(), coverage=diag.coverage(),
        field_tags=diag.selected_series(), client=_Client(amended), ticker="KO",
        fetched_at=NOW.strftime("%Y-%m-%d %H:%M UTC"),
        warnings=[*diag.warnings, f"HISTORICAL REPLAY as of {DAY}: fundamentals from {source}."],
        field_notes=diag.field_notes(), doc_diagnostics=[], company_facts=base,
        submissions=_submissions(), index_degraded=False, fresh=False,
        vintage_note="not captured (historical replay)", baseline_day=DAY,
    )
    banner = journal_reporting.replay_banner(DAY, source, date.today())
    assert out.read_text() == f"{banner}\n\n{direct}"


def test_a_replay_archives_nothing(monkeypatch, tmp_path):
    base, amended = _payloads()
    _store(base, date(2026, 5, 1))
    before = json.dumps(read_manifest(CIK))
    out, _ = _replay(monkeypatch, tmp_path, live=amended)
    assert json.dumps(read_manifest(CIK)) == before
    assert "- Vintage snapshot: not captured (historical replay)" in out.read_text()
    assert vintages.observed_vintages(CIK)[-1].captured == "2026-05-01"


def test_a_replay_needs_its_day():
    import pytest

    with pytest.raises(ValueError, match="needs the day"):
        journal_reporting.build_report("KO", replay=True)


# --- the CLIs ----------------------------------------------------------------------


def test_generate_report_as_of_delegates_to_the_replay(monkeypatch, tmp_path):
    from scripts import generate_report

    calls: list = []

    def spy(ticker, **kw):
        calls.append((ticker, kw))
        return tmp_path / "KO_2026-06-01.replay.md", "quiet"

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(journal_reporting, "build_report", spy)
    monkeypatch.setattr(generate_report.sys, "argv",
                        ["generate_report.py", "ko", "--as-of", "2026-06-01", "--no-docs"])
    assert generate_report.main() == 0
    [(ticker, kw)] = calls
    assert ticker == "KO" and kw["replay"] is True and kw["report_day"] == "2026-06-01"
    assert kw["out_dir"] == tmp_path / "reports" and kw["with_docs"] is False


def test_journal_report_replay_rebuilds_the_entry_day_and_touches_nothing(monkeypatch, tmp_path):
    import argparse

    from scripts import journal

    entry = SimpleNamespace(ticker="KO", day=DAY)
    calls: list = []
    monkeypatch.setattr(journal.store, "find_entry", lambda t, d: tmp_path / "KO_x.md")
    monkeypatch.setattr(journal.store, "is_v2", lambda p: True)
    monkeypatch.setattr(journal.store, "load_v2", lambda p: entry)
    monkeypatch.setattr(journal, "verify_lock", lambda e: True)
    monkeypatch.setattr(journal.store, "save_v2",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("entry edited")))
    monkeypatch.setattr(journal, "build_report",
                        lambda t, **kw: (calls.append((t, kw)) or (tmp_path / "x.replay.md", "q")))
    args = argparse.Namespace(ticker="KO", date=None, no_docs=True, replay=True, fresh=False)
    assert journal.cmd_report(args) == 0
    [(ticker, kw)] = calls
    assert kw["replay"] is True and kw["report_day"] == DAY.isoformat()

    monkeypatch.setattr(journal, "verify_lock", lambda e: False)
    assert journal.cmd_report(args) == 1  # a broken lock is refused, as for the real report


def test_generate_report_as_of_names_only_an_unmappable_payload_as_one(monkeypatch, tmp_path):
    """Round-9 audit F3: the replay path caught EVERY ValueError from the whole
    build and printed "could not be mapped" with exit 2, so a defect inside
    the report build read as a data problem. Only the mapping stage's own
    failure is reported that way; anything else surfaces."""
    from app.services.journal.reporting import UnmappablePayload
    from scripts import generate_report

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report.sys, "argv",
                        ["generate_report.py", "ko", "--as-of", "2026-06-01", "--no-docs"])

    def unmappable(ticker, **kw):
        raise UnmappablePayload("Could not establish at least 2 quarter-end dates for KO")

    monkeypatch.setattr(journal_reporting, "build_report", unmappable)
    assert generate_report.main() == 2

    def defect(ticker, **kw):
        raise ValueError("a defect in the report build")

    monkeypatch.setattr(journal_reporting, "build_report", defect)
    with pytest.raises(ValueError, match="a defect in the report build"):
        generate_report.main()


def test_an_unmappable_replay_payload_is_raised_as_unmappable(monkeypatch):
    from app.services.journal.reporting import UnmappablePayload

    def no_quarters(*a, **k):
        raise ValueError("Could not establish at least 2 quarter-end dates for KO")

    monkeypatch.setattr(journal_reporting, "SecClient", lambda **k: object())
    monkeypatch.setattr(journal_reporting, "replay_snapshot", no_quarters)
    with pytest.raises(UnmappablePayload, match="quarter-end dates"):
        journal_reporting.build_report("KO", replay=True, report_day="2026-06-01")
    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", no_quarters)
    with pytest.raises(UnmappablePayload, match="quarter-end dates"):
        journal_reporting.build_report("KO", vintage=False)
