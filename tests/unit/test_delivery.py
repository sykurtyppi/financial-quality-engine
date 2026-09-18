"""Delivery is best-effort and local: a notification and a drop-folder copy.
Neither may raise, and neither may touch the real drop folder from a test."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.services import delivery


class TestDropFolder:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(delivery.DROP_ENV, str(tmp_path / "drop"))
        assert delivery.drop_folder() == tmp_path / "drop"

    def test_icloud_default_only_when_present(self, monkeypatch, tmp_path):
        monkeypatch.delenv(delivery.DROP_ENV, raising=False)
        monkeypatch.setattr(delivery, "ICLOUD_DRIVE", tmp_path / "missing")
        assert delivery.drop_folder() is None
        (tmp_path / "icloud").mkdir()
        monkeypatch.setattr(delivery, "ICLOUD_DRIVE", tmp_path / "icloud")
        assert delivery.drop_folder() == tmp_path / "icloud" / delivery.DEFAULT_DROP_NAME


class TestPublish:
    def test_copies_and_overwrites_on_rebuild(self, tmp_path):
        brief = tmp_path / "NVDA_2026-08-26.md"
        brief.write_text("v1")
        dest = delivery.publish(brief, tmp_path / "drop" / "nested")
        assert dest == tmp_path / "drop" / "nested" / "NVDA_2026-08-26.md"
        assert dest.read_text() == "v1"
        brief.write_text("v2")
        assert delivery.publish(brief, tmp_path / "drop" / "nested").read_text() == "v2"

    def test_no_folder_means_none_not_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv(delivery.DROP_ENV, raising=False)
        monkeypatch.setattr(delivery, "ICLOUD_DRIVE", tmp_path / "missing")
        brief = tmp_path / "b.md"
        brief.write_text("x")
        assert delivery.publish(brief) is None


class TestNotify:
    def test_passes_title_and_message_as_argv_not_source(self, monkeypatch):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0)

        monkeypatch.delenv(delivery.NO_NOTIFY_ENV, raising=False)
        monkeypatch.setattr(delivery.shutil, "which", lambda n: "/usr/bin/osascript")
        monkeypatch.setattr(delivery.Path, "is_file", lambda self: True)
        monkeypatch.setattr(delivery.subprocess, "run", run)
        hostile = 'x" & (do shell script "rm -rf ~")'
        assert delivery.notify("T", hostile) is True
        argv = seen["argv"]
        assert argv[:2] == ["/usr/bin/osascript", "-e"]
        assert hostile in argv  # its own argv element, never inside the script
        assert hostile not in argv[2]

    def test_clips_long_messages(self, monkeypatch):
        seen = {}
        monkeypatch.delenv(delivery.NO_NOTIFY_ENV, raising=False)
        monkeypatch.setattr(delivery.shutil, "which", lambda n: "/usr/bin/osascript")
        monkeypatch.setattr(delivery.Path, "is_file", lambda self: True)
        monkeypatch.setattr(delivery.subprocess, "run",
                            lambda argv, **kw: seen.setdefault("argv", argv) and SimpleNamespace(returncode=0))
        delivery.notify("T", "word " * 200)
        assert len(seen["argv"][-1]) <= 200

    def test_never_raises(self, monkeypatch):
        monkeypatch.delenv(delivery.NO_NOTIFY_ENV, raising=False)
        monkeypatch.setattr(delivery.shutil, "which", lambda n: "/usr/bin/osascript")
        monkeypatch.setattr(delivery.Path, "is_file", lambda self: True)

        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=1)
        monkeypatch.setattr(delivery.subprocess, "run", boom)
        assert delivery.notify("T", "m") is False

    def test_opt_out_and_missing_osascript(self, monkeypatch):
        monkeypatch.setenv(delivery.NO_NOTIFY_ENV, "1")
        assert delivery.notify("T", "m") is False
        monkeypatch.delenv(delivery.NO_NOTIFY_ENV)
        monkeypatch.setattr(delivery.shutil, "which", lambda n: None)
        monkeypatch.setattr(delivery.Path, "is_file", lambda self: False)
        assert delivery.notify("T", "m") is False
