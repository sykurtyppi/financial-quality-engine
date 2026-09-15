"""The headless runs must find the Claude CLI under a scheduler's minimal
PATH, and an operator must be able to pin it explicitly."""

from __future__ import annotations

from pathlib import Path

from app.services import headless


def test_env_var_wins_over_path(monkeypatch):
    monkeypatch.setenv(headless.ENV_VAR, "/opt/claude/bin/claude")
    monkeypatch.setattr(headless.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert headless.claude_command() == "/opt/claude/bin/claude"


def test_path_lookup_when_not_pinned(monkeypatch):
    monkeypatch.delenv(headless.ENV_VAR, raising=False)
    monkeypatch.setattr(headless.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert headless.claude_command() == "/usr/local/bin/claude"


def test_falls_back_to_the_user_install_when_path_is_minimal(monkeypatch):
    # cron gives PATH=/usr/bin:/bin; the CLI installs a symlink under ~/.local/bin.
    monkeypatch.delenv(headless.ENV_VAR, raising=False)
    monkeypatch.setenv(headless.ENV_VAR, "   ")  # blank counts as unset
    monkeypatch.setattr(headless.shutil, "which", lambda name: None)
    assert headless.claude_command() == str(Path.home() / ".local" / "bin" / "claude")
