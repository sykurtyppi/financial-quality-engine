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
from contextlib import contextmanager
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


def _meanwhile(monkeypatch, change, *, by_hand: bool = False):
    """After the command's first load, another writer saves ``change`` (with
    ``by_hand``, writes it to the file as a hand edit would: the store saves
    no entry whose lock does not verify)."""
    from app.services.journal.schema_v2 import render_entry

    real = store.load_v2
    fired: list[int] = []

    def load(p):
        e = real(p)
        if not fired:
            fired.append(1)
            if by_hand:
                p.write_text(render_entry(change(real(p))), encoding="utf-8")
            else:
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
        update={"thesis": "rewritten after the fact"})}), by_hand=True)
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


_SAVE_REFUSED = {**{name: change for name, (change, _) in _REFUSED.items()},
                 "reported cleared": lambda e: e.model_copy(update={"reported": None}),
                 "reported moved": lambda e: e.model_copy(update={
                     "reported": e.reported.replace(year=2020)})}


@pytest.mark.parametrize("change", list(_SAVE_REFUSED), ids=list(_SAVE_REFUSED))
def test_a_save_over_an_entry_is_held_to_what_an_update_is(tmp_path, monkeypatch, change):
    """`save_v2(..., allow_update=True)` compared only the two `before_sha256`
    fields: it could clear a `reported` stamp, or save a BEFORE block edited
    under the stored hash, every change `update_v2` refuses (review of the
    finding-4 fix). It is held to the same check, against the entry on disk."""
    path = _seed(tmp_path, monkeypatch, reported=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        store.save_v2(_SAVE_REFUSED[change](store.load_v2(path)), path, allow_update=True)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".md")) == [path.name]


def test_a_save_that_leaves_the_lock_and_the_stamp_alone_is_saved(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    path = _seed(tmp_path, monkeypatch, reported=False)
    now = datetime.now(UTC)
    store.save_v2(store.load_v2(path).model_copy(update={"reported": now}), path,
                  allow_update=True)  # a stamp may be made
    store.save_v2(_disagree(store.load_v2(path)), path, allow_update=True)  # and kept
    entry = store.load_v2(path)
    assert entry.reported == now and entry.after.what_i_disagreed_with == "written meanwhile"


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


# --- review of the 3b fix: a report while the sweep audits the deferred one ------------
# `report --defer-mark` released the report lock once it had published, and
# the entry stayed unstamped while the sweep audited that report (minutes).
# A plain `journal.py report` in that window took the lock, found "not
# reported", built, published over the run being audited and stamped: the
# sweep's audit then failed (the live run was no longer the audited one) or
# its `mark-reported` found "already reported". A deferred report now leaves
# the entry's report PENDING (a marker beside it) until `mark-reported`; a
# plain report refuses while it is, unless told `--retry`.


def _either(tmp_path, monkeypatch, v2):
    if v2:
        return _seed(tmp_path, monkeypatch, reported=False)
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    return store.open_entry("TST", thesis="a real thesis")


def _is_reported(path):
    if store.is_v2(path):
        return store.load_v2(path).reported is not None
    return store.is_reported(path.read_text(encoding="utf-8"))


def _report_ns(path, **kw):
    import argparse

    return argparse.Namespace(**{"ticker": "TST", "date": path.stem.split("_", 1)[1],
                                 "no_docs": True, "defer_mark": False, **kw})


def _mark_ns(path):
    import argparse

    return argparse.Namespace(ticker="TST", date=path.stem.split("_", 1)[1])


def _builds(monkeypatch, cli, fail=None):
    built: list[tuple] = []

    def build(*a, **k):
        built.append(a)
        if fail is not None:
            fail()
        return Path("x.md"), "no acute signals"

    monkeypatch.setattr(cli, "build_report", build)
    return built


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_report_while_the_sweep_audits_is_refused_and_the_sweep_marks_it(
        tmp_path, monkeypatch, capsys, v2):
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0  # the sweep
    # ... which now audits that report, the report lock released ...
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path)) == 1  # the operator
    err = capsys.readouterr().err
    assert len(built) == 1, "a second report was built over the one being audited"
    assert not _is_reported(path)
    assert "being audited" in err
    # Both ways out are named as commands that run, pinned to this entry.
    import re
    import shlex

    named = [cli.build_parser().parse_args(shlex.split(c))
             for c in re.findall(r"`journal\.py ([^`]+)`", err)]
    assert [(n.cmd, n.ticker, n.date) for n in named] == [
        ("mark-reported", "TST", path.stem.split("_", 1)[1]),
        ("report", "TST", path.stem.split("_", 1)[1])]
    assert named[1].retry and not named[1].defer_mark
    assert cli.cmd_mark_reported(_mark_ns(path)) == 0  # the sweep, once the audit passed
    assert _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_retry_rebuilds_a_pending_report_on_purpose(tmp_path, monkeypatch, capsys, v2):
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid())  # the sweep that deferred it has exited
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    _by_hand(monkeypatch)
    assert cli.cmd_report(_report_ns(path, retry=True)) == 0
    assert len(built) == 2 and _is_reported(path)
    assert store.report_pending(path) is None  # the case is reported: nothing pends
    # The sweep's stamp then finds it done, as any stamp made meanwhile.
    assert cli.cmd_mark_reported(_mark_ns(path)) == 1
    assert "already reported" in capsys.readouterr().err


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_the_sweeps_own_retry_is_allowed_and_keeps_the_marker(tmp_path, monkeypatch, v2):
    """A failed audit leaves the case retryable: the next pass reports it
    again with --defer-mark, which the marker does not stop."""
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    first = store.report_pending(path)
    assert first is not None and "--defer-mark" in first
    inode = path.with_name(f".{path.name}.report.pending").stat().st_ino
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    assert len(built) == 2 and store.report_pending(path) == first
    assert path.with_name(f".{path.name}.report.pending").stat().st_ino == inode  # kept
    assert not _is_reported(path)


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_the_marker_is_there_before_the_build_starts(tmp_path, monkeypatch, v2):
    """A deferred report killed part way (its report perhaps live) leaves
    the entry pending, not open to a plain report."""
    path = _either(tmp_path, monkeypatch, v2)
    cli, seen = _cli(), []
    monkeypatch.setattr(cli, "build_report", lambda *a, **k: seen.append(
        store.report_pending(path)) or (Path("x.md"), "no acute signals"))
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    assert seen and seen[0] is not None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_deferred_report_that_published_nothing_leaves_nothing_pending(
        tmp_path, monkeypatch, v2):
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()

    def fail():
        raise RuntimeError("EDGAR down")

    _builds(monkeypatch, cli, fail)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1
    assert store.report_pending(path) is None
    built = _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path)) == 0 and len(built) == 1


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_retry_that_published_nothing_keeps_the_earlier_marker(tmp_path, monkeypatch, v2):
    """The run already published is still the one pending its audit."""
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    first = store.report_pending(path)

    def fail():
        raise RuntimeError("EDGAR down")

    _builds(monkeypatch, cli, fail)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1
    assert store.report_pending(path) == first
    assert cli.cmd_report(_report_ns(path)) == 1


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_deferred_publish_in_doubt_leaves_the_report_pending(tmp_path, monkeypatch, v2):
    """The new run may be live and was never audited: a plain report must
    not publish over it unasked."""
    from app.services.reporting.report_files import PUBLISH_IN_DOUBT_RC

    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    monkeypatch.setattr(cli, "build_report", _in_doubt)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == PUBLISH_IN_DOUBT_RC
    assert store.report_pending(path) is not None
    built = _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path)) == 1 and built == []


@pytest.mark.parametrize("when", ["before its check", "meanwhile"])
def test_a_stamp_that_is_refused_leaves_the_report_pending(tmp_path, monkeypatch, when):
    """`mark-reported` clears the marker only once it has stamped: not when
    it finds the lock broken, nor when the stamp itself is refused (the
    lock broken after its check, by hand)."""
    from app.services.journal.schema_v2 import render_entry

    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()
    _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0

    def tamper(e):
        return e.model_copy(update={"before": e.before.model_copy(
            update={"thesis": "rewritten after the fact"})})

    if when == "meanwhile":
        _meanwhile(monkeypatch, tamper, by_hand=True)
    else:
        path.write_text(render_entry(tamper(store.load_v2(path))), encoding="utf-8")
    assert cli.cmd_mark_reported(_mark_ns(path)) == 1
    assert store.report_pending(path) is not None
    assert store.load_v2(path).reported is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_plain_report_with_nothing_pending_writes_no_marker(tmp_path, monkeypatch, v2):
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path)) == 0
    assert store.report_pending(path) is None and _is_reported(path)


def test_a_marker_that_cannot_be_removed_after_the_stamp_is_said_not_raised(
        tmp_path, monkeypatch, capsys):
    """The stamp is made: `mark-reported` must not read as failed to the
    sweep, and a marker beside a stamped entry stops nothing."""
    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()
    _builds(monkeypatch, cli)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0

    def cannot(p):
        raise PermissionError(13, "read-only directory")

    monkeypatch.setattr(store, "clear_report_pending", cannot)
    assert cli.cmd_mark_reported(_mark_ns(path)) == 0
    assert "pending marker could not be removed" in capsys.readouterr().err
    assert _is_reported(path)
    assert cli.cmd_report(_report_ns(path)) == 1  # refused as reported, the marker aside


def test_the_marker_is_a_hidden_sidecar_written_whole(tmp_path, monkeypatch):
    import json

    path = _seed(tmp_path, monkeypatch, reported=False)
    _by_hand(monkeypatch)
    store.set_report_pending(path, "journal.py report --defer-mark")
    marker = path.with_name(f".{path.name}.report.pending")
    doc = json.loads(marker.read_text(encoding="utf-8"))
    assert doc["by"] == "journal.py report --defer-mark" and doc["marked"]
    owner = store.ReportOwner.loads(doc["owner"])
    assert owner == store.report_owner() and owner.pid == os.getpid() and owner.lock is None
    assert store.report_pending(path) == (
        f"marked {doc['marked']} by journal.py report --defer-mark, for "
        f"{owner.name}, pid {owner.pid} on {owner.host}")
    assert store.pending_marker(path) == store.Pending(store.report_pending(path), owner)
    handed = _owner(monkeypatch, pid=4242, lock=tmp_path / "sweep.lock")  # the sweep's child
    store.set_report_pending(path, "journal.py report --defer-mark")  # replaced whole
    assert store.pending_marker(path).owner == store.ReportOwner(**handed)
    assert store.report_pending(path).endswith(
        f", for watch.py, pid 4242 on {handed['host']}, under the sweep lock {handed['lock']}")
    assert _leftovers(tmp_path) == [] and store.list_entries() == [path]
    store.clear_report_pending(path)
    assert not marker.exists() and store.report_pending(path) is None
    store.clear_report_pending(path)  # nothing pending: nothing to do


def test_a_marker_that_cannot_be_read_still_pends(tmp_path, monkeypatch):
    """Fail closed: a marker that is there but unreadable is not "nothing"."""
    import errno

    path = _seed(tmp_path, monkeypatch, reported=False)
    store.set_report_pending(path, "journal.py report --defer-mark")
    marker = path.with_name(f".{path.name}.report.pending")
    real = os.open

    def cannot(p, *a, **k):
        if Path(p) == marker:
            raise OSError(errno.EIO, "injected")
        return real(p, *a, **k)

    monkeypatch.setattr(os, "open", cannot)
    pending = store.pending_marker(path)
    assert pending is not None and "cannot be read" in pending.text and pending.owner is None
    monkeypatch.setattr(os, "open", real)
    marker.write_bytes(b"\xff\xfe not UTF-8")
    pending = store.pending_marker(path)
    assert pending is not None and "cannot be read" in pending.text and pending.owner is None


# --- review of the pending marker: it names its OWNER, the sweep ------------------------
# The marker named the pid of the `journal.py report --defer-mark` child, which
# exits as soon as it has published: for the whole audit it protects, the pid
# it named was dead. An operator checking it read it as stale and took the
# named way out, `--retry`, which built and published over the run being
# audited and stamped it (the sweep's audit then failed, or its
# `mark-reported` found "already reported"); a `--defer-mark` run by hand did
# the same. watch.py now hands its child an owner (its own pid, host, a token
# and the sweep lock) that the marker records; `--retry` and a `--defer-mark`
# of another owner go ahead only once that owner is demonstrably gone (its pid
# not running on this host and the sweep lock free), `--retry --force` on
# purpose.

_OWNER_ENV = "FQE_REPORT_OWNER"


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _owner(monkeypatch, *, pid: int, lock: Path | None = None, host: str | None = None) -> dict:
    """What follows runs as the child of a sweep (watch.py `_generate`), whose
    identity it is handed. The format is the contract between the two."""
    import json
    import uuid

    owner = {"name": "watch.py", "pid": pid, "host": host or os.uname().nodename,
             "token": uuid.uuid4().hex, "lock": None if lock is None else str(lock)}
    monkeypatch.setenv(_OWNER_ENV, json.dumps(owner))
    return owner


def _by_hand(monkeypatch):
    """What follows is run by hand: no sweep handed it anything."""
    monkeypatch.delenv(_OWNER_ENV, raising=False)


@contextmanager
def _held(lock: Path):
    """The sweep lock, held as a running sweep or poll holds it."""
    import fcntl

    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def test_the_marker_names_the_sweep_not_its_child_and_a_retry_waits_for_it(
        tmp_path, monkeypatch, capsys):
    """rev28c_pid: the sweep's `journal.py report --defer-mark` is a child
    process, gone before the audit starts. The marker must name the sweep
    (here: this process, auditing), and `--retry` refuse while it runs."""
    path = _seed(tmp_path, monkeypatch, reported=False)
    lock = tmp_path / "sweep.lock"
    _owner(monkeypatch, pid=os.getpid(), lock=lock)
    child = textwrap.dedent(f"""\
        import importlib.util, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from app.services.journal import store
        store.ENTRIES = Path({str(tmp_path)!r})
        spec = importlib.util.spec_from_file_location("j", {str(ROOT / "scripts" / "journal.py")!r})
        j = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(j)
        j.build_report = lambda *a, **k: (Path("x.md"), "no acute signals")
        sys.argv = ["journal.py", "report", "TST", "--date", "2026-07-27", "--no-docs",
                    "--defer-mark"]
        sys.exit(j.main())
        """)
    done = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                          timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stderr
    pending = store.report_pending(path)
    assert pending is not None and f"pid {os.getpid()}" in pending and str(lock) in pending
    _by_hand(monkeypatch)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path, retry=True)) == 1  # the sweep is auditing it
    err = capsys.readouterr().err
    assert built == [] and not _is_reported(path)
    assert "pending (being audited by the sweep, or left by an interrupted run)" in err
    assert f"pid {os.getpid()}" in err and "is running" in err
    import re
    import shlex

    forced = [cli.build_parser().parse_args(shlex.split(c))
              for c in re.findall(r"`journal\.py ([^`]+)`", err)]
    assert [(n.cmd, n.date, n.retry, n.force) for n in forced] == [
        ("report", "2026-07-27", True, True)]
    assert cli.cmd_report(_report_ns(path, retry=True, force=True)) == 0  # on purpose
    assert len(built) == 1 and _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_retry_waits_while_the_sweep_lock_is_held(tmp_path, monkeypatch, capsys, v2):
    """The sweep that marked it is gone, but a sweep or poll holds the lock
    it ran under: it may be the one retrying this case. `--retry` waits."""
    path = _either(tmp_path, monkeypatch, v2)
    lock = tmp_path / "sweep.lock"
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid(), lock=lock)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    _by_hand(monkeypatch)
    capsys.readouterr()
    with _held(lock):
        assert cli.cmd_report(_report_ns(path, retry=True)) == 1
        assert f"the sweep lock {lock} is held" in capsys.readouterr().err
    assert len(built) == 1 and not _is_reported(path)
    assert cli.cmd_report(_report_ns(path, retry=True)) == 0  # released: gone
    assert len(built) == 2 and _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
@pytest.mark.parametrize("lock", ["none", "missing", "free"])
def test_a_retry_goes_ahead_once_the_owner_is_gone(tmp_path, monkeypatch, capsys, v2, lock):
    path = _either(tmp_path, monkeypatch, v2)
    lock_path = None if lock == "none" else tmp_path / "sweep.lock"
    if lock == "free":
        lock_path.write_text("")
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid(), lock=lock_path)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    _by_hand(monkeypatch)
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path)) == 1  # a plain report: refused all the same
    assert "Its owner is gone." in capsys.readouterr().err and len(built) == 1
    assert cli.cmd_report(_report_ns(path, retry=True)) == 0
    assert len(built) == 2 and _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("marker", ["another host", "garbage", "the older format",
                                    "no pid", "pid 0", "pid true", "token 7", "name 7",
                                    "host 7", "lock 7", "a list"])
def test_an_owner_that_cannot_be_checked_holds_a_retry(tmp_path, monkeypatch, capsys, marker):
    """Fail closed: an owner this host cannot check (another host, a marker
    that does not say who) is not "gone". `--retry --force` still goes."""
    import json

    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid(), host="elsewhere" if marker == "another host" else None)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    raw = path.with_name(f".{path.name}.report.pending")
    edits = {"garbage": "{not json", "the older format":
             f"marked {store.now_iso()} by journal.py report --defer-mark, pid 1 on here\n",
             "a list": "[]"}
    for field, value in (("no pid", None), ("pid 0", 0), ("pid true", True),
                         ("token 7", 7), ("name 7", 7), ("host 7", 7), ("lock 7", 7)):
        if marker == field:
            doc = json.loads(raw.read_text(encoding="utf-8"))
            doc["owner"][field.split()[-1] if field.startswith("no") else field.split()[0]] = value
            edits[marker] = json.dumps(doc)
    if marker in edits:
        raw.write_text(edits[marker], encoding="utf-8")
    _by_hand(monkeypatch)
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path, retry=True)) == 1
    err = capsys.readouterr().err
    assert "--force" in err and len(built) == 1
    assert ("cannot be checked" in err) == (marker == "another host")
    assert ("does not say who" in err) == (marker != "another host")
    assert cli.cmd_report(_report_ns(path, retry=True, force=True)) == 0
    assert len(built) == 2 and _is_reported(path)


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_defer_mark_by_hand_is_refused_while_the_sweep_audits(
        tmp_path, monkeypatch, capsys, v2):
    """rev28c_defer_by_hand: a `--defer-mark` of another owner, run while
    the sweep audits, built and published over the run being audited."""
    path = _either(tmp_path, monkeypatch, v2)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=os.getpid(), lock=tmp_path / "sweep.lock")  # auditing now
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    first = path.with_name(f".{path.name}.report.pending").read_bytes()
    _by_hand(monkeypatch)
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1
    err = capsys.readouterr().err
    assert len(built) == 1, "a second report was built over the one being audited"
    assert "being audited by the sweep" in err and f"pid {os.getpid()}" in err
    assert f"may still be at work on it (watch.py, pid {os.getpid()}, is running)" in err
    assert path.with_name(f".{path.name}.report.pending").read_bytes() == first
    for flags in ({"defer_mark": True, "force": True}, {"force": True}):  # --force: --retry's
        assert cli.cmd_report(_report_ns(path, **flags)) == 1
    assert len(built) == 1
    assert cli.cmd_mark_reported(_mark_ns(path)) == 0  # the sweep, once the audit passed
    assert _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_defer_mark_by_hand_waits_for_the_sweep_lock(tmp_path, monkeypatch, capsys, v2):
    path = _either(tmp_path, monkeypatch, v2)
    lock = tmp_path / "sweep.lock"
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid(), lock=lock)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    _by_hand(monkeypatch)
    with _held(lock):
        assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1
    assert len(built) == 1
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0  # gone: goes ahead
    assert len(built) == 2 and not _is_reported(path)


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_the_next_sweeps_retry_goes_ahead_and_takes_the_marker(tmp_path, monkeypatch, v2):
    """A failed audit (exit 4) leaves the case pending; the NEXT pass is a
    new sweep process, holding the sweep lock itself. Its retry goes ahead
    (the marker's owner is gone), and the marker then names the new sweep."""
    path = _either(tmp_path, monkeypatch, v2)
    lock = tmp_path / "sweep.lock"
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid(), lock=lock)  # the pass that failed its audit
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    now = _owner(monkeypatch, pid=os.getpid(), lock=lock)  # the next pass
    with _held(lock):
        assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    assert len(built) == 2 and not _is_reported(path)
    _by_hand(monkeypatch)
    assert cli.cmd_report(_report_ns(path, retry=True)) == 1  # the new sweep is auditing
    assert f"pid {now['pid']}" in store.report_pending(path) and len(built) == 2


@pytest.mark.parametrize("kind", ["dangling symlink", "symlink to a marker", "directory"])
def test_a_marker_that_is_not_a_regular_file_still_pends(tmp_path, monkeypatch, capsys, kind):
    """rev28c_symlink: a dangling symlink at the marker's name read as
    "nothing pending" (fail open), and every `--defer-mark` then failed to
    create the marker ("Report generation failed", for ever). Whatever is
    there that is not a regular file pends, is named, and is never followed;
    a `--defer-mark` refuses it cleanly and `--retry --force` replaces it."""
    path = _seed(tmp_path, monkeypatch, reported=False)
    marker = path.with_name(f".{path.name}.report.pending")
    if kind == "directory":
        marker.mkdir()
    else:
        target = tmp_path / "elsewhere"
        if kind == "symlink to a marker":
            _owner(monkeypatch, pid=_dead_pid())
            store.set_report_pending(target, "journal.py report --defer-mark")
            target = target.with_name(f".{target.name}.report.pending")
        os.symlink(target, marker)
    pending = store.report_pending(path)
    assert pending is not None and marker.name in pending and "not a regular file" in pending
    cli = _cli()
    built = _builds(monkeypatch, cli)
    capsys.readouterr()
    assert cli.cmd_report(_report_ns(path)) == 1
    _owner(monkeypatch, pid=_dead_pid())
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1  # the sweep's
    err = capsys.readouterr().err
    assert marker.name in err and "Report generation failed" not in err
    _by_hand(monkeypatch)
    assert cli.cmd_report(_report_ns(path, retry=True)) == 1
    assert built == [] and not _is_reported(path)
    assert cli.cmd_report(_report_ns(path, retry=True, force=True)) == 0
    assert len(built) == 1 and _is_reported(path)
    assert os.path.lexists(marker) == (kind == "directory")  # said, not raised


def test_a_marker_swapped_for_a_symlink_after_its_check_is_not_followed(tmp_path, monkeypatch):
    """The marker is opened O_NOFOLLOW: one replaced by a symlink between
    its `lstat` and its read still pends, owner unknown; one removed in
    that moment is not pending."""
    path = _seed(tmp_path, monkeypatch, reported=False)
    marker = path.with_name(f".{path.name}.report.pending")
    target = tmp_path / "other.md"
    _owner(monkeypatch, pid=_dead_pid())
    store.set_report_pending(target, "journal.py report --defer-mark")
    real_marker = target.with_name(f".{target.name}.report.pending")
    os.symlink(real_marker, marker)
    real = os.lstat
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: real(real_marker if Path(p) == marker
                                                             else p, *a, **k))
    pending = store.pending_marker(path)
    assert pending is not None and pending.owner is None and "cannot be read" in pending.text
    marker.unlink()
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: real(real_marker if Path(p) == marker
                                                             else p, *a, **k))
    assert store.pending_marker(path) is None


def test_a_marker_that_does_not_parse_is_quoted_in_part(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, reported=False)
    path.with_name(f".{path.name}.report.pending").write_text("x" * 300, encoding="utf-8")
    text = store.report_pending(path)
    assert repr("x" * 200) in text and "x" * 201 not in text


class TestIsTheOwnerAtWork:
    """`store.owner_at_work` directly: what counts as gone, what does not."""

    def _me(self, lock=None):
        return store.ReportOwner("me", os.getpid(), os.uname().nodename, "mine",
                                 None if lock is None else str(lock))

    def _gone(self, lock=None):
        return store.ReportOwner("watch.py", _dead_pid(), os.uname().nodename, "theirs",
                                 None if lock is None else str(lock))

    def test_a_pid_of_another_user_is_running(self, monkeypatch):
        def denied(pid, sig):
            raise PermissionError(1, "Operation not permitted")

        owner = self._gone()
        monkeypatch.setattr(os, "kill", denied)
        assert store.owner_at_work(owner, me=self._me()) == f"watch.py, pid {owner.pid}, is running"

    def test_a_lock_that_cannot_be_opened_cannot_be_checked(self, tmp_path):
        (tmp_path / "file").write_text("")
        owner = self._gone(tmp_path / "file" / "sweep.lock")  # ENOTDIR
        why = store.owner_at_work(owner, me=self._me())
        assert why is not None and why.startswith(f"the sweep lock {owner.lock} cannot be checked")

    def test_a_lock_that_cannot_be_probed_cannot_be_checked(self, tmp_path, monkeypatch):
        import errno
        import fcntl

        lock = tmp_path / "sweep.lock"
        lock.write_text("")

        def broken(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", broken)
        why = store.owner_at_work(self._gone(lock), me=self._me())
        assert why == (f"the sweep lock {lock} cannot be checked "
                       f"([Errno {errno.ENOLCK}] No locks available)")

    def test_another_probe_is_not_a_sweep(self, tmp_path):
        """The probe takes a SHARED lock, as a second probe does at the same
        moment: only a sweep's (exclusive) lock is "held"."""
        import fcntl

        lock = tmp_path / "sweep.lock"
        lock.write_text("")
        with open(lock) as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            assert store.owner_at_work(self._gone(lock), me=self._me()) is None
        with _held(lock):
            assert "is held" in store.owner_at_work(self._gone(lock), me=self._me())
            # ... but not by the sweep asking, whose own lock it is.
            assert store.owner_at_work(self._gone(lock), me=self._me(lock)) is None


def test_a_retry_forced_with_defer_mark_replaces_a_marker_that_is_not_a_file(
        tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, reported=False)
    marker = path.with_name(f".{path.name}.report.pending")
    os.symlink(tmp_path / "nowhere", marker)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    _owner(monkeypatch, pid=_dead_pid())
    assert cli.cmd_report(_report_ns(path, retry=True, force=True, defer_mark=True)) == 0
    assert len(built) == 1 and not _is_reported(path)
    assert not marker.is_symlink() and marker.is_file()
    assert "pid" in store.report_pending(path)


def test_a_marker_that_cannot_be_taken_back_leaves_the_builds_error_said(
        tmp_path, monkeypatch, capsys):
    """If taking back the mark raises, the build's own error is still what
    is said, and the entry stays pending (fail closed)."""
    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()

    def fail():
        raise RuntimeError("EDGAR down")

    def cannot(p):
        raise PermissionError(13, "read-only directory")

    _builds(monkeypatch, cli, fail)
    monkeypatch.setattr(store, "clear_report_pending", cannot)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 1
    err = capsys.readouterr().err
    assert "Report generation failed: EDGAR down" in err
    assert "could not be taken back" in err and "read-only directory" in err
    assert store.report_pending(path) is not None


def test_the_hints_to_report_name_the_entry(tmp_path, monkeypatch, capsys):
    """rev28c_nodate, the same omission elsewhere: a hint to run `report`
    without `--date` runs it on the ticker's NEWEST entry, whichever that is."""
    import argparse
    import re
    import shlex

    path = _seed(tmp_path, monkeypatch, reported=False)
    cli = _cli()
    capsys.readouterr()
    assert cli.cmd_after(argparse.Namespace(ticker="TST", date="2026-07-27",
                                            impact="no_value", conviction_after=None,
                                            surfaced=None, disagreed=None)) == 1
    opened = cli.cmd_open(argparse.Namespace(ticker="NEW", thesis="a thesis",
                                             conviction=None, action=None))
    assert opened == 0
    out = capsys.readouterr()
    hints = re.findall(r"`journal\.py (report [^`]+)`", out.err)
    hints += re.findall(r"^\s+journal\.py (report .+)$", out.out, re.M)
    named = [cli.build_parser().parse_args(shlex.split(h)) for h in hints]
    assert [(n.ticker, n.date) for n in named] == [
        ("TST", "2026-07-27"), ("NEW", store.find_entry("NEW").stem.split("_", 1)[1])]
    assert path.exists()


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


# --- Hermes re-audit of 84e65b0, findings 3 and 4: which report, and a stamp that fails --
# Finding 4: after a plain `report` had published, the stamp's OSError (a full
# disk, a denied write, an fsync failure, the journal folder removed) was
# uncaught: a traceback, the report live, the entry unstamped, and a retry
# that believed nothing had happened. It is now exit 9, "published, not
# stamped", naming the generation and the command that stamps it, and the
# entry is left pending so a plain retry refuses instead of building again.
# Finding 3: the sweep audited the NEWEST report of the ticker, not the one its
# `report --defer-mark` child published, and stamped the entry for it. The
# child now says which report it published (`--result-file`), and
# `mark-reported --generation` stamps only that run.


def _publishing(monkeypatch, cli, tmp_path):
    """A build that publishes for real (`report_files.replacing`), as
    `build_report` does, into a reports directory of the test's own (the
    one `reporting.report_path` names, which `mark-reported --generation`
    reads)."""
    from app.services.journal import reporting
    from app.services.reporting.report_files import replacing

    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    monkeypatch.setattr(reporting, "REPORTS", reports)
    built: list[Path] = []

    def build(ticker, with_docs=True, report_day=None, fresh=False, **k):
        out = reports / f"{ticker}_{report_day}.md"
        with replacing(out) as staged:
            staged.report.write_text(f"# {ticker} {report_day}, run {len(built)}\n")
            staged.ledger.write_text("{}")
        built.append(out)
        return out, "no acute signals"

    monkeypatch.setattr(cli, "build_report", build)
    return reports, built


def _live(out: Path):
    from app.services.reporting.report_files import read_live

    return read_live(out)


def _stamp_fails(monkeypatch, path: Path, how: str) -> None:
    """The entry's stamp (`update_v2` for v2, `mark_reported` for v1), and
    only its first attempt, fails as ``how``; the publish before it and the
    pending marker after it are written as usual."""
    import errno
    import shutil
    from unittest import mock

    name = "update_v2" if store.is_v2(path) else "mark_reported"
    real = getattr(store, name)
    fired: list[int] = []

    def raiser(err):
        def fail(*a, **k):
            raise err
        return fail

    def stamp(p, *a, **k):
        if fired:
            return real(p, *a, **k)
        fired.append(1)
        if how == "ENOSPC":
            with mock.patch.object(store, "_durable_write",
                                   raiser(OSError(errno.ENOSPC, "No space left on device"))):
                return real(p, *a, **k)
        if how == "EACCES":
            with mock.patch.object(os, "replace",
                                   raiser(PermissionError(errno.EACCES, "Permission denied"))):
                return real(p, *a, **k)
        if how == "fsync":
            with mock.patch.object(os, "fsync", raiser(OSError(errno.EIO, "fsync failed"))):
                return real(p, *a, **k)
        if how == "folder removed":
            shutil.rmtree(p.parent)
            return real(p, *a, **k)
        if how == "defect":
            raise TypeError("a programming error in the stamp")
        raise AssertionError(how)

    monkeypatch.setattr(store, name, stamp)


def _hinted(cli, err: str):
    import re
    import shlex

    return [cli.build_parser().parse_args(shlex.split(h))
            for h in re.findall(r"`(?:python scripts/)?journal\.py ([^`]+)`", err)]


@pytest.mark.parametrize("how", ["ENOSPC", "EACCES", "fsync", "folder removed"])
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_stamp_that_fails_after_the_publish_is_exit_9_and_recoverable(
        tmp_path, monkeypatch, capsys, v2, how):
    path = _either(tmp_path / "entries", monkeypatch, v2)
    day = path.stem.split("_", 1)[1]
    cli = _cli()
    _by_hand(monkeypatch)
    _, built = _publishing(monkeypatch, cli, tmp_path)
    _stamp_fails(monkeypatch, path, how)
    assert cli.cmd_report(_report_ns(path)) == 9
    err = capsys.readouterr().err
    run = _live(built[0])
    assert "Traceback" not in err
    assert f"the report WAS published: generation {run.generation_id} at {run.report}" in err
    assert "the entry is NOT stamped reported" in err
    (hint,) = [n for n in _hinted(cli, err) if n.cmd == "mark-reported"]
    assert (hint.ticker, hint.date, hint.generation) == ("TST", day, run.generation_id)
    assert "`python scripts/journal.py mark-reported TST" in err
    if how == "folder removed":
        # Nothing to stamp, nor to leave pending: said, not hidden.
        assert "its pending marker could not be written either" in err
        assert not path.exists()
        return
    assert "left PENDING" in err
    assert not _is_reported(path)
    assert store.pending_marker(path).generation_id == run.generation_id
    # A plain retry refuses, naming the command that stamps the published run.
    assert cli.cmd_report(_report_ns(path)) == 1 and len(built) == 1
    err = capsys.readouterr().err
    (again,) = [n for n in _hinted(cli, err) if n.cmd == "mark-reported"]
    assert again.generation == run.generation_id
    assert cli.cmd_mark_reported(hint) == 0
    assert _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_defect_in_the_stamp_is_exit_9_with_its_traceback(tmp_path, monkeypatch, capsys, v2):
    """Anything but the disk (OSError) or the entry (ValueError) is a defect:
    still "published, not stamped", and its traceback is printed, not hidden."""
    path = _either(tmp_path / "entries", monkeypatch, v2)
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    _stamp_fails(monkeypatch, path, "defect")
    assert cli.cmd_report(_report_ns(path)) == 9
    err = capsys.readouterr().err
    assert "Traceback" in err and "TypeError: a programming error in the stamp" in err
    assert f"generation {_live(built[0]).generation_id}" in err
    assert store.pending_marker(path) is not None and not _is_reported(path)


def test_a_stamp_that_fails_after_a_build_it_cannot_identify_says_so(
        tmp_path, monkeypatch, capsys):
    """A build that published nothing this command could see (no generation
    recorded): the hint cannot pin one, and says what it can."""
    path = _either(tmp_path / "entries", monkeypatch, True)
    cli = _cli()
    _builds(monkeypatch, cli)
    _stamp_fails(monkeypatch, path, "ENOSPC")
    assert cli.cmd_report(_report_ns(path)) == 9
    err = capsys.readouterr().err
    assert "the report WAS published: x.md (its generation could not be read)" in err
    (hint,) = [n for n in _hinted(cli, err) if n.cmd == "mark-reported"]
    assert hint.generation is None
    assert store.pending_marker(path).generation_id is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_mark_reported_says_a_stamp_that_fails_without_a_traceback(
        tmp_path, monkeypatch, capsys, v2):
    import argparse

    path = _either(tmp_path / "entries", monkeypatch, v2)
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    gid = _live(built[0]).generation_id
    ns = argparse.Namespace(ticker="TST", date=path.stem.split("_", 1)[1], generation=gid)
    _stamp_fails(monkeypatch, path, "ENOSPC")
    capsys.readouterr()
    assert cli.cmd_mark_reported(ns) == 1
    err = capsys.readouterr().err
    assert "NOT stamped" in err and "No space left on device" in err
    assert not _is_reported(path) and store.pending_marker(path).generation_id == gid
    assert cli.cmd_mark_reported(ns) == 0  # the disk freed: the same command
    assert _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_mark_reported_stamps_only_the_generation_it_is_told(tmp_path, monkeypatch, capsys, v2):
    import argparse

    path = _either(tmp_path / "entries", monkeypatch, v2)
    day = path.stem.split("_", 1)[1]
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    gid = _live(built[0]).generation_id
    assert store.pending_marker(path).generation_id == gid
    capsys.readouterr()
    assert cli.cmd_mark_reported(argparse.Namespace(ticker="TST", date=day,
                                                    generation="f" * 32)) == 1
    err = capsys.readouterr().err
    assert f"NOT stamped: generation {'f' * 32} is not this entry's report" in err
    assert not _is_reported(path) and store.pending_marker(path).generation_id == gid
    assert cli.cmd_mark_reported(argparse.Namespace(ticker="TST", date=day, generation=gid)) == 0
    assert _is_reported(path) and store.report_pending(path) is None


@pytest.mark.parametrize("by", ["marker", "live report"])
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_mark_reported_takes_the_marker_or_the_live_report_as_the_entrys_run(
        tmp_path, monkeypatch, v2, by):
    """The marker records the run its command published; the live report
    named for the entry is the entry's run too. Neither: refused."""
    import argparse

    path = _either(tmp_path / "entries", monkeypatch, v2)
    day = path.stem.split("_", 1)[1]
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    assert cli.cmd_report(_report_ns(path, defer_mark=True)) == 0
    audited = _live(built[0]).generation_id
    cli.build_report("TST", report_day=day)  # published over it meanwhile, by another run
    later = _live(built[0]).generation_id
    assert later != audited

    def mark(gid):
        return cli.cmd_mark_reported(argparse.Namespace(ticker="TST", date=day, generation=gid))

    # An old-format marker (no generation recorded) leaves only the live report.
    store.set_report_pending(path, "journal.py report --defer-mark")
    assert store.pending_marker(path).generation_id is None
    assert mark(audited) == 1 and not _is_reported(path)
    if by == "marker":
        store.set_report_pending(path, "journal.py report --defer-mark",
                                 generation_id=audited, report=str(built[0]))
        assert mark(audited) == 0
    else:
        assert mark(later) == 0
    assert _is_reported(path) and store.report_pending(path) is None


def test_an_old_format_marker_still_reads(tmp_path, monkeypatch):
    import json

    path = _seed(tmp_path, monkeypatch, reported=False)
    handed = _owner(monkeypatch, pid=4242)
    marker = path.with_name(f".{path.name}.report.pending")
    marker.write_text(json.dumps({"marked": "2026-09-01T00:00:00Z",
                                  "by": "journal.py report --defer-mark", "owner": handed}))
    pending = store.pending_marker(path)
    assert pending.owner == store.ReportOwner(**handed)
    assert (pending.generation_id, pending.report) == (None, None)
    assert pending.text == (f"marked 2026-09-01T00:00:00Z by journal.py report --defer-mark, "
                            f"for watch.py, pid 4242 on {handed['host']}")
    store.set_report_pending(path, "journal.py report --defer-mark",
                             generation_id="a" * 32, report="reports/TST_2026-07-27.md")
    pending = store.pending_marker(path)
    assert (pending.generation_id, pending.report) == ("a" * 32, "reports/TST_2026-07-27.md")
    assert pending.text.endswith(f"; its report: generation {'a' * 32} "
                                 "(reports/TST_2026-07-27.md)")


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_deferred_report_writes_its_result_file(tmp_path, monkeypatch, v2):
    import json

    from app.services.reporting.report_files import live_name

    path = _either(tmp_path / "entries", monkeypatch, v2)
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    held = tmp_path / "held"
    held.mkdir()
    result = held / "result.json"
    assert cli.cmd_report(_report_ns(path, defer_mark=True, result_file=str(result))) == 0
    run = _live(built[0])
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "report": str(run.report), "generation_id": run.generation_id, "ticker": "TST",
        "entry_day": path.stem.split("_", 1)[1],
        "before_sha256": store.load_v2(path).before_sha256 if v2 else None}
    # The generation's own report, never the live name another run can take.
    assert run.report != built[0] and live_name(run.report) == built[0]
    assert sorted(p.name for p in held.iterdir()) == ["result.json"]  # no temporary left


@pytest.mark.parametrize("v2", [True, False], ids=["v2", "v1"])
def test_a_build_that_fails_writes_no_result_file(tmp_path, monkeypatch, v2):
    path = _either(tmp_path / "entries", monkeypatch, v2)
    cli = _cli()

    def fail():
        raise RuntimeError("EDGAR down")

    _builds(monkeypatch, cli, fail)
    result = tmp_path / "result.json"
    assert cli.cmd_report(_report_ns(path, defer_mark=True, result_file=str(result))) == 1
    assert not result.exists()


def test_a_result_file_that_cannot_be_written_is_published_not_stamped(
        tmp_path, monkeypatch, capsys):
    """Written whole or not at all: a failed write leaves no file (and no
    temporary), and the command says the report is live, exit 9."""
    import errno
    from unittest import mock

    path = _either(tmp_path / "entries", monkeypatch, True)
    cli = _cli()
    _, built = _publishing(monkeypatch, cli, tmp_path)
    held = tmp_path / "held"
    held.mkdir()
    real = cli.write_atomic

    def torn(p, text, **k):
        def fail(fd):
            raise OSError(errno.EIO, "fsync failed")
        with mock.patch.object(os, "fsync", fail):
            return real(p, text, **k)

    monkeypatch.setattr(cli, "write_atomic", torn)
    rc = cli.cmd_report(_report_ns(path, defer_mark=True, result_file=str(held / "r.json")))
    err = capsys.readouterr().err
    assert rc == 9 and list(held.iterdir()) == []
    assert f"the report WAS published: generation {_live(built[0]).generation_id}" in err
    assert "could not be written" in err and "fsync failed" in err
    assert store.pending_marker(path) is not None and not _is_reported(path)


@pytest.mark.parametrize("extra", [{}, {"replay": True, "defer_mark": True}])
def test_a_result_file_is_only_for_a_deferred_report(tmp_path, monkeypatch, capsys, extra):
    path = _either(tmp_path / "entries", monkeypatch, True)
    cli = _cli()
    built = _builds(monkeypatch, cli)
    before = path.read_bytes()
    rc = cli.cmd_report(_report_ns(path, result_file=str(tmp_path / "r.json"), **extra))
    assert rc == 1 and built == [] and path.read_bytes() == before
    assert "--result-file" in capsys.readouterr().err
    assert cli.build_parser().parse_args(
        ["report", "TST", "--defer-mark", "--result-file", "r.json"]).result_file == "r.json"


def test_a_marker_rewrite_that_fails_leaves_the_earlier_marker(tmp_path, monkeypatch):
    """A marker is rewritten in place (recording the run once published):
    replaced whole, never removed first, so a write that fails leaves the
    entry pending as it was, not unmarked."""
    import errno

    path = _seed(tmp_path, monkeypatch, reported=False)
    store.set_report_pending(path, "journal.py report --defer-mark")
    before = store.pending_marker(path)

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store, "_durable_write", full)
    with pytest.raises(OSError):
        store.set_report_pending(path, "journal.py report --defer-mark",
                                 generation_id="a" * 32, report="r.md")
    assert store.pending_marker(path) == before
