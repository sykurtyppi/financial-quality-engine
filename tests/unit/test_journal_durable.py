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
