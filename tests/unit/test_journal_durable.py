"""Journal writes are whole or not at all, and updates never lose each other.

Hermes (audit of the stack at b2780be) interrupted a journal write and got a
55-byte entry cut to the 12 bytes "reported: 20": every store write was a
plain `Path.write_text`, which truncates the entry before writing it. That
path runs on every successful report (`mark_reported`), every AFTER/OUTCOME
edit (`set_field`) and every resolve/report update (`save_v2`).

The interruption is reproduced faithfully rather than by timing: a child
process whose file-size limit (RLIMIT_FSIZE) is 12 bytes, with SIGXFSZ
ignored, has its write cut short with EFBIG exactly as a crash mid-write or
a full disk would — the old code left the 12-byte entry Hermes saw.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.journal import store

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 12  # bytes: Hermes' truncated entry

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("resource"), "RLIMIT_FSIZE"), reason="needs RLIMIT_FSIZE (POSIX)")


def _run_cut_short(body: str, entries: Path) -> subprocess.CompletedProcess:
    """Run ``body`` in a child whose writes stop at LIMIT bytes."""
    script = textwrap.dedent(f"""\
        import resource, signal, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from app.services.journal import store
        store.ENTRIES = Path({str(entries)!r})
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, ({LIMIT}, {LIMIT}))
        """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def _leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".tmp"))


@pytest.fixture
def entry(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    return store.open_entry("NVDA", thesis="data-center demand holds through FY27")


class TestAWriteCutShortLeavesTheEntryWhole:
    def test_mark_reported(self, entry, tmp_path):
        before = entry.read_bytes()
        proc = _run_cut_short(f"store.mark_reported(Path({str(entry)!r}))", tmp_path)
        assert proc.returncode != 0, "the write was supposed to be cut short"
        assert entry.read_bytes() == before, "the entry was truncated"
        assert _leftovers(tmp_path) == []

    def test_set_field(self, entry, tmp_path):
        before = entry.read_bytes()
        proc = _run_cut_short(
            f"store.set_field(Path({str(entry)!r}), 'what_it_surfaced', 'receivables outgrew revenue')",
            tmp_path)
        assert proc.returncode != 0
        assert entry.read_bytes() == before
        assert _leftovers(tmp_path) == []

    def test_save_v2_update(self, tmp_path, monkeypatch):
        from datetime import UTC, date, datetime

        from app.services.journal.schema_v2 import (
            Assumption,
            BeforeBlock,
            EntryV2,
            lock_entry,
        )

        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        locked = lock_entry(EntryV2(
            ticker="MXL", day=date(2026, 7, 27),
            opened=datetime(2026, 7, 27, 9, 41, 11, tzinfo=UTC),
            before=BeforeBlock(
                thesis="one of three optical DSP suppliers", conviction=4,
                intended_action="hold",
                assumptions=[Assumption(metric="revenue", comparator=">", threshold=1e9,
                                        window="FY2026Q2", source="10-Q",
                                        resolve_by=date(2026, 8, 15))]),
        ))
        path = store.save_v2(locked)
        before = path.read_bytes()
        proc = _run_cut_short(
            f"p = Path({str(path)!r}); store.save_v2(store.load_v2(p), p, allow_update=True)",
            tmp_path)
        assert proc.returncode != 0
        assert path.read_bytes() == before
        assert _leftovers(tmp_path) == []

    def test_open_entry_leaves_no_partial_entry(self, tmp_path):
        proc = _run_cut_short("store.open_entry('AMKR', thesis='packaging demand')", tmp_path)
        assert proc.returncode != 0
        assert not list(tmp_path.glob("AMKR_*.md")), "a partial entry was left behind"
        assert _leftovers(tmp_path) == []


def test_an_update_keeps_the_entrys_permissions(entry):
    os.chmod(entry, 0o600)
    store.set_field(entry, "what_it_surfaced", "x")
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    store.mark_reported(entry)
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600


def test_two_writers_never_lose_each_others_update(entry, tmp_path):
    """The sweep (mark_reported), the web UI (set_field) and journal.py can
    update one entry at once. Each read the entry, changed its own line and
    wrote the whole file back, so the slower writer erased the faster one's
    change. A per-entry lock now serialises the read-modify-write."""
    script = textwrap.dedent(f"""\
        import re, sys, time
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from app.services.journal import store
        real = store.re.subn
        def slow(*a, **k):
            out = real(*a, **k)
            time.sleep(0.5)  # between the read and the write
            return out
        store.re.subn = slow
        store.set_field(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
        """)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(entry), key, value])
             for key, value in (("what_it_surfaced", "margins"), ("what_i_disagreed_with", "capex"))]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    text = entry.read_text()
    assert "what_it_surfaced: margins" in text
    assert "what_i_disagreed_with: capex" in text


def test_a_create_never_replaces_an_entry_that_appeared_meanwhile(entry):
    """`open_entry` checked, then wrote: an entry created between the two was
    overwritten. The create is now a hard link, which refuses."""
    before = entry.read_bytes()
    with pytest.raises(FileExistsError):
        store._durable_write(entry, "# replaced\n", create=True)
    assert entry.read_bytes() == before
    assert _leftovers(entry.parent) == []


def test_a_new_entry_takes_its_mode_from_the_umask(tmp_path, monkeypatch):
    """Review of the fix: every new entry was chmod-ed 0o644, so a journal kept
    private with `umask 077` became readable by every user on the host.
    `write_text` let the umask decide; so does the new code."""
    from datetime import UTC, date, datetime

    from app.services.journal.schema_v2 import (
        Assumption,
        BeforeBlock,
        EntryV2,
        lock_entry,
    )

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    old = os.umask(0o077)
    try:
        v1 = store.open_entry("NVDA", thesis="private")
        v2 = store.save_v2(lock_entry(EntryV2(
            ticker="MXL", day=date(2026, 7, 27), opened=datetime(2026, 7, 27, tzinfo=UTC),
            before=BeforeBlock(thesis="private too", conviction=3, intended_action="hold",
                               assumptions=[Assumption(
                                   metric="revenue", comparator=">", threshold=1.0,
                                   window="FY2026Q2", source="10-Q",
                                   resolve_by=date(2026, 8, 15))]))))
    finally:
        os.umask(old)
    assert stat.S_IMODE(v1.stat().st_mode) == 0o600
    assert stat.S_IMODE(v2.stat().st_mode) == 0o600
    old = os.umask(0o022)
    try:
        v1b = store.open_entry("AMKR", thesis="shared")
    finally:
        os.umask(old)
    assert stat.S_IMODE(v1b.stat().st_mode) == 0o644


def test_entries_are_read_as_the_utf_8_they_are_written_in(tmp_path):
    """Review of the fix: entries are now written as UTF-8 but were read in
    the locale's encoding. Under a non-UTF-8 locale the dash in every
    entry's header broke `tally` (the dashboard) and `mark_reported`."""
    script = textwrap.dedent(f"""\
        import locale, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        if locale.getpreferredencoding(False).lower().replace("-", "") == "utf8":
            sys.exit(3)  # this platform cannot run a non-UTF-8 locale
        from app.services.journal import store
        store.ENTRIES = Path({str(tmp_path)!r})
        thesis = "caf\\u00e9 capex \\u2014 \\u20ac2bn"
        p = store.open_entry("NVDA", thesis=thesis)
        store.mark_reported(p)
        store.set_field(p, "what_it_surfaced", "na\\u00efve margins")
        e = store.parse_entry(p)
        assert e["thesis"] == thesis, ascii(e["thesis"])
        assert e["what_it_surfaced"] == "na\\u00efve margins"
        assert e["is_reported"]
        store.tally()
        """)
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("LANG", None)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, env=env)
    if proc.returncode == 3:
        pytest.skip("no non-UTF-8 locale here")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("err", ["EPERM", "EOPNOTSUPP", "EXDEV"])
def test_a_create_where_no_hard_link_can_be_made_still_works(tmp_path, monkeypatch, err):
    """FAT/exFAT and some SMB/FUSE mounts refuse hard links. The create falls
    back to a rename after an existence check — still whole, still
    no-clobber (every creator holds the entry lock)."""
    import errno

    def no_links(src, dst):
        raise OSError(getattr(errno, err), "no hard links here")

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    monkeypatch.setattr(store.os, "link", no_links)
    p = store.open_entry("NVDA", thesis="works on a USB stick")
    assert store.parse_entry(p)["thesis"] == "works on a USB stick"
    before = p.read_bytes()
    with pytest.raises(FileExistsError):
        store._durable_write(p, "# replaced\n", create=True)
    assert p.read_bytes() == before
    assert _leftovers(tmp_path) == []


def test_any_other_link_failure_is_raised(tmp_path, monkeypatch):
    import errno

    def full(src, dst):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    monkeypatch.setattr(store.os, "link", full)
    with pytest.raises(OSError, match="disk full"):
        store.open_entry("NVDA", thesis="x")
    assert list(tmp_path.glob("*.md")) == []
    assert _leftovers(tmp_path) == []


# --- v2 updates: the entry a command saves is the entry on disk now ----------------
# journal.py loaded an entry, checked it, and saved its own copy. A write by
# another process in between (the sweep's `mark-reported` beside a user's
# `resolve --commit`) was erased. Each command below loads the entry, then
# another writer changes it on disk, then the command saves.

_journal = None


def _cli():
    global _journal
    if _journal is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("journal_cli_durable",
                                                      ROOT / "scripts" / "journal.py")
        _journal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_journal)
    return _journal


def _seed(tmp_path, monkeypatch, *, reported: bool, metrics: tuple[str, ...] = ("revenue",),
          threshold: float = 1.0):
    from datetime import UTC, date, datetime

    from app.services.journal.schema_v2 import (
        Assumption,
        BeforeBlock,
        EntryV2,
        lock_entry,
    )

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    entry = lock_entry(EntryV2(
        ticker="TST", day=date(2026, 7, 27), opened=datetime(2026, 7, 27, 9, tzinfo=UTC),
        before=BeforeBlock(thesis="a real thesis", conviction=3, intended_action="hold",
                           assumptions=[Assumption(metric=m, comparator=">",
                                                   threshold=threshold, window="FY2026Q2",
                                                   source="10-Q",
                                                   resolve_by=date(2026, 8, 15))
                                        for m in metrics])))
    if reported:
        entry = entry.model_copy(update={"reported": datetime.now(UTC)})
    return store.save_v2(entry)


def _meanwhile(monkeypatch, change):
    """After the command's first load, another writer saves ``change``."""
    real = store.load_v2
    fired: list[int] = []

    def load(p):
        e = real(p)
        if not fired:
            fired.append(1)
            store.save_v2(change(real(p)), p, allow_update=True)
        return e

    monkeypatch.setattr(store, "load_v2", load)


def _disagree(e):
    return e.model_copy(update={"after": e.after.model_copy(
        update={"what_i_disagreed_with": "written meanwhile"})})


class TestV2UpdatesKeepAWriteMadeMeanwhile:
    def _kept(self, path):
        assert store.load_v2(path).after.what_i_disagreed_with == "written meanwhile"

    def test_mark_reported(self, tmp_path, monkeypatch):
        import argparse
        path = _seed(tmp_path, monkeypatch, reported=False)
        _meanwhile(monkeypatch, _disagree)
        assert _cli().cmd_mark_reported(argparse.Namespace(ticker="TST", date="2026-07-27")) == 0
        assert store.load_v2(path).reported is not None
        self._kept(path)

    def test_report(self, tmp_path, monkeypatch):
        import argparse
        path = _seed(tmp_path, monkeypatch, reported=False)
        cli = _cli()
        monkeypatch.setattr(cli, "build_report",
                            lambda *a, **k: (Path("x.md"), "no acute signals"))
        _meanwhile(monkeypatch, _disagree)
        assert cli.cmd_report(argparse.Namespace(ticker="TST", date="2026-07-27",
                                                 no_docs=True, defer_mark=False)) == 0
        assert store.load_v2(path).reported is not None
        self._kept(path)

    def test_after(self, tmp_path, monkeypatch):
        import argparse
        path = _seed(tmp_path, monkeypatch, reported=True)
        _meanwhile(monkeypatch, _disagree)
        assert _cli().cmd_after(argparse.Namespace(
            ticker="TST", date="2026-07-27", impact=None, conviction_after=None,
            surfaced="margins", disagreed=None)) == 0
        assert store.load_v2(path).after.what_it_surfaced == "margins"
        self._kept(path)

    def test_outcome(self, tmp_path, monkeypatch):
        import argparse
        path = _seed(tmp_path, monkeypatch, reported=True)
        _meanwhile(monkeypatch, _disagree)
        assert _cli().cmd_outcome(argparse.Namespace(
            ticker="TST", date="2026-07-27", outcome_date=None, what_happened="beat",
            verdict=None, y=None)) == 0
        assert store.load_v2(path).outcome.what_happened == "beat"
        self._kept(path)

    def test_resolve(self, tmp_path, monkeypatch):
        import argparse

        from app.services.formulas import registry
        from app.services.ingestion import edgar_adapter
        from app.services.journal import resolver
        from app.services.journal.schema_v2 import Resolution

        path = _seed(tmp_path, monkeypatch, reported=True)
        monkeypatch.setattr(edgar_adapter, "fetch_dataset", lambda t: (None, None))
        monkeypatch.setattr(registry, "compute_metrics", lambda ds: None)
        monkeypatch.setattr(resolver, "propose_resolution",
                            lambda a, ds, b, assumption_index: Resolution(
                                assumption_index=assumption_index, state="met", observed=2.0))
        _meanwhile(monkeypatch, _disagree)
        assert _cli().cmd_resolve(argparse.Namespace(ticker="TST", date="2026-07-27",
                                                     commit=True)) == 0
        assert [r.state for r in store.load_v2(path).resolutions] == ["met"]
        self._kept(path)


def test_mark_reported_refuses_a_stamp_made_meanwhile(tmp_path, monkeypatch):
    """The re-read entry is re-checked: a `reported` stamp another writer made
    in between is kept, and the command says it was already reported."""
    import argparse
    from datetime import UTC, datetime

    path = _seed(tmp_path, monkeypatch, reported=False)
    first = datetime.now(UTC)
    _meanwhile(monkeypatch, lambda e: e.model_copy(update={"reported": first}))
    assert _cli().cmd_mark_reported(argparse.Namespace(ticker="TST", date="2026-07-27")) == 1
    assert store.load_v2(path).reported == first


def test_outcome_refuses_a_verdict_recorded_meanwhile(tmp_path, monkeypatch):
    """Factual OUTCOME fields are immutable once set; one set by another
    writer after the command's check is still refused, not overwritten."""
    import argparse

    path = _seed(tmp_path, monkeypatch, reported=True)
    _meanwhile(monkeypatch, lambda e: e.model_copy(update={"outcome": e.outcome.model_copy(
        update={"verdict": "helped"})}))
    assert _cli().cmd_outcome(argparse.Namespace(
        ticker="TST", date="2026-07-27", outcome_date=None, what_happened=None,
        verdict="hurt", y=None)) == 1
    assert store.load_v2(path).outcome.verdict == "helped"


def test_an_update_refuses_a_before_block_tampered_meanwhile(tmp_path, monkeypatch):
    """The command verified the lock on the entry it loaded; the entry it now
    changes is re-read, so its lock is verified again. A BEFORE block edited
    in between is refused, never stamped as reported."""
    import argparse

    path = _seed(tmp_path, monkeypatch, reported=False)
    _meanwhile(monkeypatch, lambda e: e.model_copy(update={"before": e.before.model_copy(
        update={"thesis": "rewritten after the fact"})}))
    assert _cli().cmd_mark_reported(argparse.Namespace(ticker="TST", date="2026-07-27")) == 1
    entry = store.load_v2(path)
    assert entry.reported is None
    with pytest.raises(store.UpdateRefused, match="LOCK BROKEN"):
        store.update_v2(path, lambda e: e)


def test_resolve_commits_only_terminal_proposals_and_says_what_it_left(
        tmp_path, monkeypatch, capsys):
    """`pending` is never written (it would close the assumption); the
    command names what it committed and what it left for retry."""
    import argparse

    from app.services.formulas import registry
    from app.services.ingestion import edgar_adapter
    from app.services.journal import resolver
    from app.services.journal.schema_v2 import Resolution

    path = _seed(tmp_path, monkeypatch, reported=True, metrics=("revenue", "cfo", "net_income"))
    monkeypatch.setattr(edgar_adapter, "fetch_dataset", lambda t: (None, None))
    monkeypatch.setattr(registry, "compute_metrics", lambda ds: None)
    monkeypatch.setattr(resolver, "propose_resolution",
                        lambda a, ds, b, assumption_index: Resolution(
                            assumption_index=assumption_index,
                            state="met" if assumption_index == 0 else "pending",
                            observed=2.0, note="from the 10-Q" if assumption_index == 0 else None))
    assert _cli().cmd_resolve(argparse.Namespace(ticker="TST", date="2026-07-27",
                                                 commit=True)) == 0
    out = capsys.readouterr().out
    assert "note: from the 10-Q" in out
    assert "committed 1 terminal resolution(s)" in out
    assert "left 2 pending for retry" in out
    assert [(r.assumption_index, r.state) for r in store.load_v2(path).resolutions] == [(0, "met")]


# --- Hermes audit of 424b0b4, finding 4: `update_v2` saves only a locked entry ---------
# `update_v2` verified the lock of the entry it READ, then saved whatever the
# callback returned; the save compared only the `before_sha256` fields. A
# callback that edited the BEFORE block (keeping the stored hash) wrote an
# entry whose lock no longer verified; one that moved the ticker or day wrote
# a file whose name no longer matched its entry.


def _rehashed(e):
    """A BEFORE block edited and re-hashed: consistent with itself, and a
    different preregistration from the one on disk."""
    from app.services.journal.schema_v2 import hash_before

    before = e.before.model_copy(update={"thesis": "rewritten after the fact"})
    return e.model_copy(update={"before": before, "before_sha256": hash_before(before)})


def _in_place(e):
    """The same, done to the entry ``update_v2`` handed over and returned as
    itself: compared with that object, nothing would look changed."""
    from app.services.journal.schema_v2 import hash_before

    e.before.thesis = "rewritten in place"
    e.before_sha256 = hash_before(e.before)
    return e


_REFUSED = {  # what the callback does -> (it, what the refusal names)
    "before thesis, stored hash kept": (lambda e: e.model_copy(update={
        "before": e.before.model_copy(update={"thesis": "rewritten after the fact"})}),
        "before"),
    "before and its hash": (_rehashed, "before, before_sha256"),
    "before, in place": (_in_place, "before, before_sha256"),
    "before_sha256": (lambda e: e.model_copy(update={"before_sha256": "0" * 64}),
                      "before_sha256"),
    "locked_at": (lambda e: e.model_copy(update={"locked_at": e.locked_at.replace(year=2020)}),
                  "locked_at"),
    "opened": (lambda e: e.model_copy(update={"opened": e.opened.replace(year=2020)}), "opened"),
    "ticker": (lambda e: e.model_copy(update={"ticker": "OTHER"}), "ticker"),
    "day": (lambda e: e.model_copy(update={"day": e.day.replace(day=28)}), "day"),
}


@pytest.mark.parametrize("change", list(_REFUSED), ids=list(_REFUSED))
def test_an_update_that_changes_the_locked_entry_is_refused_and_writes_nothing(
        tmp_path, monkeypatch, change):
    path = _seed(tmp_path, monkeypatch, reported=False)
    before = path.read_bytes()
    callback, named = _REFUSED[change]
    with pytest.raises(store.UpdateRefused, match=f"the update changed {named}, which an "
                       "update must leave as it is"):
        store.update_v2(path, callback)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".md")) == [path.name]


def test_an_update_that_leaves_the_lock_alone_is_saved(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from app.services.journal.schema_v2 import verify_lock

    path = _seed(tmp_path, monkeypatch, reported=False)
    now = datetime.now(UTC)
    saved = store.update_v2(path, lambda e: _disagree(e).model_copy(update={"reported": now}))
    entry = store.load_v2(path)
    assert entry == saved and verify_lock(entry) and entry.reported == now
    assert entry.after.what_i_disagreed_with == "written meanwhile"


def test_a_before_block_that_compares_equal_but_hashes_differently_is_refused(
        tmp_path, monkeypatch):
    """Equal is not enough: -0.0 == 0.0, but the hash reads the JSON, where
    they differ. The lock itself is verified on what would be saved."""
    path = _seed(tmp_path, monkeypatch, reported=False, threshold=0.0)
    before = path.read_bytes()

    def negative_zero(e):
        a = e.before.assumptions[0].model_copy(update={"threshold": -0.0})
        return e.model_copy(update={"before": e.before.model_copy(update={"assumptions": [a]})})

    with pytest.raises(store.UpdateRefused, match="changed the BEFORE block"):
        store.update_v2(path, negative_zero)
    assert path.read_bytes() == before


# --- Hermes audit of 424b0b4, finding 3b: two `report` commands on one entry ----------
# Each command checked "not reported", built and published its report, then
# stamped. Run twice at once on one unstamped entry, both built and published;
# the second's stamp was refused ("already reported", exit 1) with its report
# the live one (v1 stamped twice and said nothing). A report command now holds
# the entry's REPORT lock from the check through the stamp: the second waits,
# then finds the entry stamped and refuses before building anything.

_REPORTER = textwrap.dedent("""\
    import importlib.util, os, sys, time
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from app.services.journal import store
    store.ENTRIES = Path({entries!r})
    spec = importlib.util.spec_from_file_location("journal_cli", {journal!r})
    journal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(journal)
    calls = Path({calls!r})

    def build(ticker, **kw):
        (calls / str(os.getpid())).write_text(ticker)
        # Wait a while (never forever) for the rival to build too: unlocked it
        # does, and both publish; locked it is waiting for this command.
        deadline = time.monotonic() + 3
        while len(list(calls.iterdir())) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        return Path("x.md"), "no acute signals"

    journal.build_report = build
    sys.argv = ["journal.py", *sys.argv[1:]]
    sys.exit(journal.main())
    """)


def _race(tmp_path, *argv):
    calls = tmp_path / "calls"
    calls.mkdir()
    script = _REPORTER.format(root=str(ROOT), entries=str(store.ENTRIES), calls=str(calls),
                              journal=str(ROOT / "scripts" / "journal.py"))
    procs = [subprocess.Popen([sys.executable, "-c", script, *argv], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
             for _ in range(2)]
    done = [(p.wait(timeout=60), *p.communicate()) for p in procs]
    return sorted(done), sorted(p.name for p in calls.iterdir())


def test_two_v2_report_commands_build_once(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, reported=False)
    done, built = _race(tmp_path, "report", "TST", "--date", "2026-07-27", "--no-docs")
    assert [rc for rc, _, _ in done] == [0, 1], done
    assert "already generated" in done[1][2]
    assert len(built) == 1, "the refused command built (and published) a report"
    assert store.load_v2(path).reported is not None


def test_two_v1_report_commands_build_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    path = store.open_entry("TST", thesis="a real thesis")
    done, built = _race(tmp_path, "report", "TST", "--no-docs")
    assert [rc for rc, _, _ in done] == [0, 1], done
    assert "already generated" in done[1][2]
    assert len(built) == 1
    assert store.is_reported(path.read_text(encoding="utf-8"))


def _stamp(path):
    """Stamp ``path`` as a rival report command would have."""
    from datetime import UTC, datetime

    if store.is_v2(path):
        store.update_v2(path, lambda e: e.model_copy(update={"reported": datetime.now(UTC)}))
    else:
        store.mark_reported(path)


def _stamped_while_waiting(monkeypatch, path):
    """The report lock, as a command that waited for it finds it: the
    command it waited for has stamped the entry meanwhile."""
    from contextlib import contextmanager

    @contextmanager
    def lock(p):
        assert p == path
        _stamp(path)
        yield

    monkeypatch.setattr(store, "report_lock", lock, raising=False)


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
@pytest.mark.parametrize("defer", [False, True], ids=["stamp", "defer-mark"])
def test_the_report_lock_is_taken_before_the_reported_check(tmp_path, monkeypatch, v2, defer):
    import argparse

    if v2:
        path = _seed(tmp_path, monkeypatch, reported=False)
    else:
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        path = store.open_entry("TST", thesis="a real thesis")
    cli, built = _cli(), []
    monkeypatch.setattr(cli, "build_report", lambda *a, **k: built.append(a) or (
        Path("x.md"), "no acute signals"))
    _stamped_while_waiting(monkeypatch, path)
    assert cli.cmd_report(argparse.Namespace(
        ticker="TST", date=path.stem.split("_", 1)[1], no_docs=True, defer_mark=defer)) == 1
    assert built == []


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_mark_reported_waits_for_a_report_in_progress(tmp_path, monkeypatch, v2):
    """The sweep's `mark-reported` beside a `report` still building: it
    stamped first, and the report command's own stamp was then refused with
    its report live. It now waits for the report lock, then finds the stamp."""
    import argparse
    import threading

    if v2:
        path = _seed(tmp_path, monkeypatch, reported=False)
    else:
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        path = store.open_entry("TST", thesis="a real thesis")
    cli, order = _cli(), []
    ns = argparse.Namespace(ticker="TST", date=path.stem.split("_", 1)[1])
    marker = threading.Thread(target=lambda: order.append(cli.cmd_mark_reported(ns)))
    with store.report_lock(path):  # a report command, building
        marker.start()
        marker.join(0.5)
        assert marker.is_alive(), "mark-reported did not wait for the report in progress"
        _stamp(path)
        order.append("stamped by the report command")
    marker.join(30)
    assert order == ["stamped by the report command", 1]


def test_a_deferred_report_leaves_the_case_retryable_and_markable(tmp_path, monkeypatch):
    """The watch flow: `report --defer-mark`, the audit, then `mark-reported`.
    The report lock is released when the deferred command returns."""
    import argparse

    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()
    monkeypatch.setattr(cli, "build_report", lambda *a, **k: (Path("x.md"), "no acute signals"))
    ns = argparse.Namespace(ticker="TST", date="2026-07-27", no_docs=True, defer_mark=True)
    assert cli.cmd_report(ns) == 0 and cli.cmd_report(ns) == 0  # retryable
    assert store.load_v2(path).reported is None
    assert cli.cmd_mark_reported(argparse.Namespace(ticker="TST", date="2026-07-27")) == 0
    assert store.load_v2(path).reported is not None


# --- follow-up: a `reported` stamp, once made, stays -----------------------------------
# The "no peek-then-edit" invariant: a report is generated once. `update_v2`
# refused any change to the lock, but a callback could still clear the
# stamp (and the case could be reported again) or move it.


@pytest.mark.parametrize("change", ["cleared", "moved"])
def test_an_update_cannot_clear_or_move_a_reported_stamp(tmp_path, monkeypatch, change):
    from datetime import timedelta

    path = _seed(tmp_path, monkeypatch, reported=True)
    before = path.read_bytes()
    with pytest.raises(store.UpdateRefused, match="the update changed reported,"):
        store.update_v2(path, lambda e: e.model_copy(update={
            "reported": None if change == "cleared" else e.reported + timedelta(hours=1)}))
    assert path.read_bytes() == before


def test_a_reported_stamp_is_still_set_once_and_can_be_kept(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    path = _seed(tmp_path, monkeypatch, reported=False)
    now = datetime.now(UTC)
    store.update_v2(path, lambda e: e.model_copy(update={"reported": now}))
    # An update that keeps the stamp as it is (every AFTER/OUTCOME edit) is saved.
    store.update_v2(path, _disagree)
    entry = store.load_v2(path)
    assert entry.reported == now and entry.after.what_i_disagreed_with == "written meanwhile"


# --- follow-up: a publish in doubt is said, with its own exit code ----------------------
# `PublishInDoubt` (the new generation may be live) reached the report
# commands' `except Exception` and was printed as an ordinary "Report
# generation failed" (exit 1): the same code as a build that published
# nothing, so neither cron nor the operator could tell the live report might
# be the new one.


def _in_doubt(*a, **k):
    from app.services.reporting.report_files import PublishInDoubt

    raise PublishInDoubt("TST_2026-07-27.md: publishing g failed (OSError: eio), and switching "
                         "back failed (OSError: eio): the NEW generation g may be live.")


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
@pytest.mark.parametrize("mode", ["stamp", "defer-mark", "replay"])
def test_a_publish_in_doubt_exits_with_its_own_code_and_stamps_nothing(
        tmp_path, monkeypatch, capsys, v2, mode):
    import argparse

    from app.services.reporting.report_files import PUBLISH_IN_DOUBT_RC

    if v2:
        path = _seed(tmp_path, monkeypatch, reported=False)
    else:
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        path = store.open_entry("TST", thesis="a real thesis")
    before = path.read_bytes()
    cli = _cli()
    monkeypatch.setattr(cli, "build_report", _in_doubt)
    rc = cli.cmd_report(argparse.Namespace(
        ticker="TST", date=path.stem.split("_", 1)[1], no_docs=True,
        defer_mark=mode == "defer-mark", replay=mode == "replay"))
    err = capsys.readouterr().err
    assert rc == PUBLISH_IN_DOUBT_RC == 8
    assert "the NEW generation g may be live" in err and "Report generation failed" not in err
    assert path.read_bytes() == before  # never stamped
