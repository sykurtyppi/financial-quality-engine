"""Where the Claude Code CLI lives for the headless runs (audit, brief).

The scripts shell out to `claude -p`. Interactively that resolves through the
user's shell PATH; a scheduler (cron, launchd) does not source that profile
and gives a minimal PATH — on this project's reference machine the CLI is a
symlink under ~/.local/bin, so the bare name fails there. A bare name is also
the one thing on the command line an attacker with a writable PATH entry could
substitute. Resolution order, explicit over discovered:

    CLAUDE_BIN env var  >  `claude` on PATH  >  ~/.local/bin/claude
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_VAR = "CLAUDE_BIN"
FALLBACK = Path.home() / ".local" / "bin" / "claude"


def claude_command() -> str:
    """Absolute path (or explicit operator choice) for the CLI. Never raises:
    a missing binary surfaces as the subprocess FileNotFoundError the callers
    already report."""
    explicit = os.environ.get(ENV_VAR, "").strip()
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    return str(FALLBACK)
