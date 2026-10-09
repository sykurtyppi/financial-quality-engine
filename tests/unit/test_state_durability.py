"""State files whose loss matters are durable: the file and its folder are
fsynced after the rename (review of 6bf9f9e, L6). The watchlist is the
sweep's calendar and its thesis pins; the audit-attempt counter caps the
paid audit runs. A rename alone can be lost on power loss."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from app.services.watch import watchlist as wl

ROOT = Path(__file__).resolve().parents[2]


def _fsynced(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = os.fsync

    def fsync(fd):
        seen.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return seen


def test_the_watchlist_is_written_durably(tmp_path, monkeypatch):
    seen = _fsynced(monkeypatch)
    path = tmp_path / "watchlist.json"
    wl._atomic_write(path, {"watchlist": []})
    assert seen[-1] == str(tmp_path)  # the folder, after the rename
    assert any(p.startswith(str(tmp_path / "watchlist.json")) for p in seen[:-1])  # the file
    assert path.read_text().startswith("{")


def test_the_audit_attempt_counter_is_written_durably(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("watch_cli_durable", ROOT / "scripts" / "watch.py")
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
    report = tmp_path / "KO_2026-10-09.md"
    report.write_text("# r\n")
    monkeypatch.setattr(watch, "_run_audit", lambda r: 4)
    seen = _fsynced(monkeypatch)
    assert watch._run_audit_capped(report) == (4, False)
    assert (tmp_path / "KO_2026-10-09_audit.attempts").read_text() == "1\n"
    assert seen[-1] == str(tmp_path)
