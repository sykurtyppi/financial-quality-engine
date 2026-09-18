"""Where a finished brief goes, and how the operator hears about it.

The sweep runs unattended; without this module a completed print is
indistinguishable from a quiet night unless someone opens a terminal. Two
channels, both local, neither publishing anything (the repo is public and
`reports/` deliberately is not):

- `notify`: a macOS notification (osascript), which a launchd job in the
  login session can post. Best-effort — never raises, returns False when
  it could not be sent.
- `publish`: copy the brief into a drop folder readable from a phone. Default
  is an "Earnings Briefs" folder in iCloud Drive when iCloud Drive exists on
  this Mac; `FQE_BRIEF_DROP=<dir>` points anywhere else (a Dropbox folder,
  a synced notes directory). Returns the copied path, or None when there is
  nowhere to copy to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

DROP_ENV = "FQE_BRIEF_DROP"
NO_NOTIFY_ENV = "FQE_NO_NOTIFY"
ICLOUD_DRIVE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
DEFAULT_DROP_NAME = "Earnings Briefs"
NOTIFY_TIMEOUT_S = 10.0
_MAX_NOTIFY_CHARS = 200


def drop_folder() -> Path | None:
    """The configured drop folder, or None when none is available."""
    explicit = os.environ.get(DROP_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if ICLOUD_DRIVE.is_dir():
        return ICLOUD_DRIVE / DEFAULT_DROP_NAME
    return None


def publish(brief: Path, dest_root: Path | None = None) -> Path | None:
    """Copy `brief` into the drop folder (created if needed). Same filename,
    overwritten on rebuild so the phone always shows the latest version."""
    root = dest_root if dest_root is not None else drop_folder()
    if root is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    dest = root / brief.name
    shutil.copyfile(brief, dest)
    return dest


def _clip(s: str, limit: int = _MAX_NOTIFY_CHARS) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def notify(title: str, message: str) -> bool:
    """Post a macOS notification. Never raises; False when not delivered
    (not macOS, osascript missing, FQE_NO_NOTIFY set, or the call failed)."""
    if os.environ.get(NO_NOTIFY_ENV):
        return False
    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    if not Path(osascript).is_file():
        return False
    # Values are passed as argv to a tiny script, never interpolated into
    # AppleScript source: a brief headline can contain any quote it likes.
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        proc = subprocess.run(
            # `--` ends option parsing: a title or message starting with "-e"
            # is an argument to the script, never a second script fragment.
            [osascript, "-e", script, "--", _clip(title, 80), _clip(message)],
            capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
