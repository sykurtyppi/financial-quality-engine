"""A symlink planted at a name the engine writes never makes it write outside
its own directories (Hermes audit of 424b0b4, finding 5, as reproduced).

The finding as stated (a report-pointer cleanup deleting a link's outside
target) did not reproduce: nothing deletes a resolved target. What did is
WRITE-THROUGH: a link at a name the engine writes (the reports' generation
pointer, a lock sidecar, a state file, a brief's source file, the drop
folder copy, a holder's assumptions file) made the engine truncate, append
to, overwrite or create the file the link points at.

This matters only where another principal can create links in ``reports/``,
``journal/``, the vintage store, the brief work directories or the drop
folder; there a link at a name the engine writes must fail loudly or be
replaced as a link, never followed. Each test plants a link to a victim
outside every engine directory, runs the real function, and checks the
victim is byte-for-byte what it was, with its mode, and that a dangling
link's target was not created.
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import io
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.services.reporting import report_files as rf
from app.services.reporting.report_files import (
    GENERATIONS_DIR,
    STAGING_DIR,
    current_generation,
    generations,
    link_audit,
    read_live,
    replacing,
    restore,
    set_aside,
)

ROOT = Path(__file__).resolve().parents[2]
NAME = "AAPL_2026-09-26.md"
BASE = NAME.removesuffix(".md")


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"symlink_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_audit = _script("run_audit")
watch_cli = _script("watch")
brief_cli = _script("earnings_brief")


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    monkeypatch.setenv(rf.ENGINE_ENV, "0123456789ab")
    rf.engine_commit.cache_clear()
    yield
    rf.engine_commit.cache_clear()


@pytest.fixture
def outside(tmp_path):
    """Where the victims live: no engine directory is under it."""
    out = tmp_path / "outside"
    out.mkdir()
    return out


def _victim(outside: Path, name: str, body: str = "VICTIM\n") -> Path:
    v = outside / name
    v.write_text(body)
    os.chmod(v, 0o640)  # a mode no engine write would give it
    return v


def _plant(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def _intact(victim: Path, body: str = "VICTIM\n") -> bool:
    return (not victim.is_symlink() and victim.read_text() == body
            and victim.stat().st_mode & 0o777 == 0o640)


def _tree(root: Path) -> dict[str, object]:
    """Every entry under ``root``, links not followed: a link's target, a
    file's bytes and mode, or "dir"."""
    out: dict[str, object] = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = Path(d) / n
            out[str(p.relative_to(root))] = (
                ("link", os.readlink(p)) if p.is_symlink()
                else "dir" if p.is_dir() else (p.read_bytes(), p.stat().st_mode & 0o777))
    return out


def _publish(reports: Path, tag: str = "first") -> Path:
    report = reports / NAME
    with replacing(report) as staged:
        staged.ledger.write_text(json.dumps({"run": tag}))
        staged.report.write_text(f"# {tag} report\n")
    return report


def _home(reports: Path) -> Path:
    return reports / GENERATIONS_DIR / BASE


# --- the reports' generation pointer ---------------------------------------------


def _outside_run(outside: Path) -> list[Path]:
    """Files named as a run's, in the directory the pointer is aimed at."""
    return [_victim(outside, n, f"VICTIM {n}\n")
            for n in (NAME, f"{BASE}.ledger.json", f"{BASE}_audit.md")]


class TestPointer:
    """``.generations/<base>/current`` is a link this engine writes with one
    plain name, its generation's. Aimed anywhere else, `current_generation`
    returned it unchecked: `read_live` pinned the outside directory,
    `publish_audit` wrote ``<base>_audit.md`` into it (mode 0o444, over a
    file already there), and `set_aside` handed back its files as the run's."""

    @pytest.mark.parametrize("aim", ["absolute", "relative", "dotdot", "dot", "nested",
                                     "linked-dir", "file", "missing"])
    def test_a_pointer_this_engine_did_not_write_is_refused(self, tmp_path, outside, aim):
        reports = tmp_path / "reports"
        report = _publish(reports)
        home = _home(reports)
        victims = _outside_run(outside)
        target = {
            "absolute": str(outside),
            "relative": os.path.relpath(outside, home),
            "dotdot": "..",
            "dot": ".",
            "nested": f"{current_generation(report).name}/../../../../outside",
            "linked-dir": "kept",
            "file": "a-file",
            "missing": "20260926T210507Z_0009_gone",
        }[aim]
        os.symlink(outside, home / "kept")
        (home / "a-file").write_text("x")
        rf._symlink(home / rf.CURRENT, target)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            current_generation(report)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            read_live(report)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            set_aside(report)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            link_audit(report)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            _publish(reports, "second")
        assert all(_intact(v, f"VICTIM {v.name}\n") for v in victims)
        assert sorted(p.name for p in outside.iterdir()) == sorted(v.name for v in victims)
        # `restore` is the way back: it writes the pointer anew.
        (gen,) = generations(report)
        assert restore(report, gen.name) == gen / NAME
        assert read_live(report).text.startswith("# first report")

    def test_publish_audit_never_writes_through_the_pointer(self, tmp_path, outside):
        """The reproducer: the run's report placed outside, the pointer aimed
        at it, then the headless audit's publish."""
        reports = tmp_path / "reports"
        report = reports / NAME
        home = _home(reports)
        home.mkdir(parents=True)
        victims = _outside_run(outside)
        os.symlink(outside, home / rf.CURRENT)
        rf._link_live_names(report)
        with pytest.raises(OSError, match="not a pointer this engine wrote"):
            run_audit.publish_audit(report, read_live(report), "AUDIT BODY\n")
        assert all(_intact(v, f"VICTIM {v.name}\n") for v in victims)

    def test_a_linked_generation_named_by_path_is_refused(self, tmp_path, outside):
        """Given a generation's own path, `read_live` read it as that
        generation even when the directory was a link out: an audit of it
        was written beside the outside files."""
        reports = tmp_path / "reports"
        _publish(reports)
        victims = _outside_run(outside)
        os.symlink(outside, _home(reports) / "20260926T210507Z_0009_kept")
        with pytest.raises(OSError, match="not a generation this engine wrote"):
            read_live(_home(reports) / "20260926T210507Z_0009_kept" / NAME)
        assert all(_intact(v, f"VICTIM {v.name}\n") for v in victims)

    @pytest.mark.parametrize("linked", [GENERATIONS_DIR, f"{GENERATIONS_DIR}/{BASE}",
                                        STAGING_DIR])
    def test_a_linked_directory_of_its_own_is_refused(self, tmp_path, outside, linked):
        """The pointer check holds only in the engine's own directory: with
        ``.generations`` or ``.generations/<base>`` a link out, the pointer
        and the generation it names were wherever the link points (a real
        directory, a plain name), and an audit was written there; with
        ``.staging`` a link out, every build was. The run's own files are
        moved out and the directory linked to them: each operation is
        refused (ELOOP) and nothing out there changes."""
        reports = tmp_path / "reports"
        report = _publish(reports)
        gen_name = current_generation(report).name
        away = outside / "away"
        os.rename(reports / linked, away)
        os.symlink(away, reports / linked)
        before = _tree(outside)
        ops = [lambda: _publish(reports, "second"), lambda: set_aside(report),
               lambda: restore(report, gen_name)]
        if linked != STAGING_DIR:
            ops += [lambda: current_generation(report), lambda: read_live(report),
                    lambda: read_live(_home(reports) / gen_name / NAME),
                    lambda: link_audit(report), lambda: generations(report)]
        for op in ops:
            with pytest.raises(OSError) as e:
                op()
            assert _eloop(e)
        assert _tree(outside) == before

    def test_real_generations_still_read_by_path(self, tmp_path):
        reports = tmp_path / "reports"
        report = _publish(reports)
        gen = current_generation(report)
        _publish(reports, "second")
        live = read_live(gen / NAME)
        assert live.generation_dir == gen and live.text.startswith("# first report")
        # A path that names no generation on disk reads as no run, as before.
        assert read_live(_home(reports) / "20260926T210507Z_0009_gone" / NAME) is None


# --- lock sidecars ---------------------------------------------------------------


def _eloop(excinfo) -> bool:
    return excinfo.value.errno == errno.ELOOP


class TestLocks:
    """A lock opened ``open(p, "w")`` truncated a linked file; one opened
    with O_CREAT but no O_NOFOLLOW created a dangling link's target. Each
    now fails loudly (ELOOP) and writes nothing."""

    def test_publish_lock(self, tmp_path, outside):
        reports = tmp_path / "reports"
        dangling = outside / "created_by_lock"
        _plant(reports / STAGING_DIR / f"{BASE}.lock", dangling)
        with pytest.raises(OSError) as e:
            _publish(reports)
        assert _eloop(e) and not dangling.exists()

    def test_journal_entry_lock(self, tmp_path, outside):
        from app.services.journal import store

        entry = tmp_path / "journal" / "AAPL_2026-09-26.md"
        entry.parent.mkdir()
        entry.write_text("reported:\n")
        dangling = outside / "created_by_lock"
        _plant(entry.with_name(f".{entry.name}.lock"), dangling)
        with pytest.raises(OSError) as e:
            store.mark_reported(entry)
        assert _eloop(e) and not dangling.exists()
        assert entry.read_text() == "reported:\n"

    def test_watchlist_lock(self, tmp_path, outside):
        from app.services.watch import watchlist as wl

        p = tmp_path / "journal" / "watchlist.json"
        p.parent.mkdir()
        p.write_text('{"watchlist": []}')
        v = _victim(outside, "watchlist_lock")
        _plant(p.with_name("watchlist.json.lock"), v)
        with pytest.raises(OSError) as e:
            wl.add_entry({"ticker": "AAPL", "print_at": "2026-10-30T20:30:00Z"}, p)
        assert _eloop(e) and _intact(v)
        assert p.read_text() == '{"watchlist": []}'

    def test_vintage_cik_lock(self, tmp_path, outside):
        from app.services.ingestion import vintages as vg

        root = tmp_path / "vintages"
        v = _victim(outside, "vintage_lock")
        _plant(vg.cik_dir(320193, root) / vg.LOCK, v)
        with pytest.raises(OSError) as e:
            vg.store_snapshot(320193, {"facts": {}}, root=root)
        assert _eloop(e) and _intact(v)
        assert vg.list_vintages(320193, root) == []

    def test_sweep_activity_lock(self, tmp_path, outside, monkeypatch):
        lock = tmp_path / "journal" / "sweep.lock"
        monkeypatch.setattr(watch_cli, "SWEEP_LOCK", lock)
        v = _victim(outside, "sweep_lock")
        _plant(lock, v)
        with pytest.raises(OSError) as e, watch_cli._activity_lock(timeout=0):
            pass
        assert _eloop(e) and _intact(v)

    def test_sec_cache_publication_lock(self, tmp_path, outside):
        from app.services.ingestion import sec_client

        entry = tmp_path / "cache" / "companyfacts_CIK0000320193.json"
        dangling = outside / "created_by_lock"
        _plant(entry.with_name(f".{entry.name}.lock"), dangling)
        with pytest.raises(OSError) as e, sec_client._publication_lock(entry):
            pass
        assert _eloop(e) and not dangling.exists()

    def test_real_locks_still_lock(self, tmp_path):
        """No link: each lock is taken and its sidecar left as before."""
        from app.services.ingestion import sec_client
        from app.services.journal import store
        from app.services.watch import watchlist as wl

        reports = tmp_path / "reports"
        _publish(reports)
        entry = tmp_path / "e.md"
        entry.write_text("reported:\n")
        store.mark_reported(entry)
        p = tmp_path / "watchlist.json"
        wl.add_entry({"ticker": "AAPL", "print_at": "2026-10-30T20:30:00Z"}, p)
        with sec_client._publication_lock(tmp_path / "x.json"):
            pass
        for lock in (reports / STAGING_DIR / f"{BASE}.lock", tmp_path / ".e.md.lock",
                     tmp_path / "watchlist.json.lock", tmp_path / ".x.json.lock"):
            assert lock.is_file() and not lock.is_symlink(), lock


# --- files written whole: a link at the name is replaced, never followed -----------


class TestReplacedNotFollowed:
    def test_overdue_alert_state(self, tmp_path, outside, monkeypatch):
        state = tmp_path / "journal" / ".overdue_alerted.json"
        monkeypatch.setattr(watch_cli, "OVERDUE_ALERTS", state)
        v = _victim(outside, "overdue")
        _plant(state, v)
        assert watch_cli._overdue_to_alert(["AAPL"], date(2026, 9, 29)) == ["AAPL"]
        assert _intact(v) and not state.is_symlink()
        assert json.loads(state.read_text()) == {"AAPL": "2026-09-29"}
        # Read back: already reported today.
        assert watch_cli._overdue_to_alert(["AAPL"], date(2026, 9, 29)) == []

    def test_digest(self, tmp_path, outside, monkeypatch, capsys):
        briefs = tmp_path / "reports" / "briefs"
        briefs.mkdir(parents=True)
        today = date.today().isoformat()
        (briefs / f"AAPL_{today}.md").write_text("# AAPL\n## Headline\nx\n")
        monkeypatch.setattr(brief_cli, "BRIEFS", briefs)
        digest = briefs / f"DIGEST_{today}.md"
        v = _victim(outside, "digest")
        _plant(digest, v)
        assert brief_cli.cmd_digest(argparse.Namespace(since=None, out=None)) == 0
        assert _intact(v) and not digest.is_symlink()
        assert digest.read_text().startswith("# Earnings digest")

    def test_drop_folder_copy(self, tmp_path, outside):
        from app.services.delivery import publish

        brief = tmp_path / "AAPL_2026-09-26.md"
        brief.write_text("BRIEF\n")
        drop = tmp_path / "drop"
        v = _victim(outside, "drop")
        _plant(drop / brief.name, v)
        assert publish(brief, drop) == drop / brief.name
        assert _intact(v) and not (drop / brief.name).is_symlink()
        assert (drop / brief.name).read_text() == "BRIEF\n"
        assert sorted(p.name for p in drop.iterdir()) == [brief.name]

    def test_brief_source_files(self, tmp_path, outside, monkeypatch):
        from app.services.brief import sources as bs
        from app.services.ingestion import edgar_documents as ed
        from tests.unit import test_earnings_brief as teb

        files = {"k-new-index-headers.html": teb.HEADER,
                 "k-old-index-headers.html": teb.HEADER.replace("q2pr.htm", "q1pr.htm"),
                 "q1pr.htm": teb.LONG, "q2pr.htm": teb.LONG, "cfo.htm": teb.LONG,
                 "slides.htm": teb.LONG, "short.htm": "<p>x</p>"}
        monkeypatch.setattr(ed, "_fetch_archive", lambda c, cik, acc, doc: files[doc])
        monkeypatch.setattr(bs, "_fetch_archive", lambda c, cik, acc, doc: files[doc])
        asm_root = tmp_path / "assumptions"
        asm_root.mkdir()
        (asm_root / "NVDA.md").write_text("- DC revenue grows\n")
        transcripts = tmp_path / "transcripts"
        (transcripts / "NVDA").mkdir(parents=True)
        (transcripts / "NVDA" / "2026-08-26.txt").write_text("Operator: welcome.")
        out_root = tmp_path / "reports" / "briefs"
        wd = out_root / "NVDA" / "2026-08-26"
        names = ("release_EX-99_1.txt", "exhibit_EX-99_2.txt", "prior_release.txt",
                 "transcript.txt", "assumptions.txt")
        victims = {n: _victim(outside, n) for n in names}
        for n, v in victims.items():
            _plant(wd / n, v)
        src = bs.collect_sources(teb._Client(), "NVDA", out_root=out_root,
                                 transcript_root=transcripts, assumptions_root=asm_root,
                                 derive=False)
        assert sorted(f.path.name for f in src.files) == sorted(names)
        for n, v in victims.items():
            assert _intact(v), n
            assert not (wd / n).is_symlink(), n
        assert (wd / "transcript.txt").read_text() == "Operator: welcome."
        assert "DC revenue grows" in (wd / "assumptions.txt").read_text()

    def test_price_cache(self, tmp_path, outside, monkeypatch):
        from app.services.backtesting import prices

        body = {"chart": {"result": [{"timestamp": [1704205800],
                                      "indicators": {"adjclose": [{"adjclose": [185.0]}]}}]}}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(prices.urllib.request, "urlopen",
                            lambda req, timeout: _Resp(json.dumps(body).encode()))
        cache = tmp_path / "data" / "cache" / "prices"
        cache.mkdir(parents=True)
        entry = cache / "AAPL_2024-01-01_2024-02-01.json"
        dangling = outside / "created_by_cache"
        _plant(entry, dangling)
        series = prices.PriceClient(cache).fetch("AAPL", date(2024, 1, 1), date(2024, 2, 1))
        assert series is not None and series.closes == [185.0]
        assert not dangling.exists() and not entry.is_symlink()
        assert json.loads(entry.read_text()) == body


# --- the vintage store's own files -----------------------------------------------


class TestVintageStore:
    def test_temporary_names_are_not_predictable(self, tmp_path, outside):
        """The manifest's and the snapshot's temporary names were the digest
        and the pid: a link planted at one was truncated and filled with the
        store's bytes, then renamed into the store as the manifest."""
        from app.services.ingestion import vintages as vg

        root = tmp_path / "vintages"
        facts = {"facts": {"x": 1}}
        now = datetime(2026, 9, 29, 15, tzinfo=UTC)
        d = vg.cik_dir(320193, root)
        snap = d / vg.snapshot_name(now.astimezone().date(), vg.digest_of(facts))
        man = d / vg.MANIFEST
        v_man = _victim(outside, "manifest_tmp")
        v_snap = _victim(outside, "snapshot_tmp")
        _plant(man.with_name(f".{man.name}.{os.getpid()}.tmp"), v_man)
        _plant(snap.with_name(f".{snap.name}.{os.getpid()}.tmp"), v_snap)
        cap = vg.store_snapshot(320193, facts, now=now, root=root)
        assert cap.reason == "captured" and cap.path == snap
        assert _intact(v_man) and _intact(v_snap)
        for p in (snap, man):
            assert p.is_file() and not p.is_symlink(), p
        assert vg.read_manifest(320193, root)["snapshots"][0]["file"] == snap.name

    def test_problem_day_marker(self, tmp_path, outside):
        from app.services.ingestion import vintages as vg

        root = tmp_path / "vintages"
        dangling = outside / "created_by_marker"
        v = _victim(outside, "marker")
        _plant(vg.cik_dir(320193, root) / f"{vg.BUSY_PREFIX}2026-09-28", dangling)
        _plant(vg.cik_dir(320193, root) / f"{vg.BUSY_PREFIX}2026-09-29", v)
        before = v.stat().st_mtime_ns
        vg._record_problem_day(320193, date(2026, 9, 28), root)
        vg._record_problem_day(320193, date(2026, 9, 29), root)
        assert not dangling.exists() and _intact(v) and v.stat().st_mtime_ns == before
        # A real day is still recorded.
        vg._record_problem_day(320193, date(2026, 9, 30), root)
        assert (vg.cik_dir(320193, root) / f"{vg.BUSY_PREFIX}2026-09-30").is_file()


# --- a holder's assumptions file -------------------------------------------------


class TestAssumptions:
    def test_a_linked_file_is_not_appended_to(self, tmp_path, outside):
        from app.services.brief import assumptions as asm

        root = tmp_path / "journal" / "assumptions"
        v = _victim(outside, "NVDA.md")
        _plant(root / "NVDA.md", v)
        with pytest.raises(OSError) as e:
            asm.add_assumption("NVDA", "DC revenue grows", root=root)
        assert _eloop(e) and _intact(v)

    def test_a_dangling_link_is_not_created(self, tmp_path, outside):
        from app.services.brief import assumptions as asm

        root = tmp_path / "journal" / "assumptions"
        dangling = outside / "created.md"
        _plant(root / "NVDA.md", dangling)
        with pytest.raises(OSError) as e:
            asm.add_assumption("NVDA", "DC revenue grows", root=root)
        assert _eloop(e) and not dangling.exists()

    def test_the_file_is_written_as_before(self, tmp_path):
        from app.services.brief import assumptions as asm

        root = tmp_path / "journal" / "assumptions"
        p = asm.add_assumption("NVDA", "DC revenue grows", root=root)
        asm.add_assumption("NVDA", "no dilution", root=root)
        asm.add_assumption("NVDA", "no dilution", root=root)
        assert p.read_text() == ("# NVDA — standing assumptions\n"
                                 "# One per bullet. Each brief reports held / challenged / "
                                 "no news.\n\n- DC revenue grows\n- no dilution\n")
