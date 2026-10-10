"""What stops a first run, said on the home page before the operator types a
ticker rather than as a failed run after it.

Two things: the SEC identity (`sec_client` refuses to fetch without one,
and SEC's fair-access rule wants a name and an email in it), and whether
the folder a run publishes to (``reports/workbench/``) can be written,
asked of the OS with `os.access` — which answers for this process's user
and for a read-only mount (EROFS) alike — and never by creating a file: a
page view writes nothing (review of 9d00328; the probe file it made on
every GET was a write behind a read). A missing `[web]` extra is not
listed: the page could not render without it, and `scripts/ui.py` says
what to install.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.workbench import views

IDENTITY_ENV = "EDGAR_IDENTITY"
# "Name email": one or more words, then an address. Loose on purpose: SEC
# asks for a name and a contact, not a format.
_IDENTITY_RE = re.compile(r"^\S+(?:\s+\S+)*\s+[^\s@]+@[^\s@]+\.[^\s@]+$")


def _identity_problem() -> str | None:
    value = os.environ.get(IDENTITY_ENV)
    if not value:
        return (f"{IDENTITY_ENV} is not set. SEC requires a name and an email on every request: "
                f'stop the workbench, run export {IDENTITY_ENV}="Your Name you@example.com", '
                "and start it again (python scripts/ui.py).")
    if not _IDENTITY_RE.match(value.strip()):
        return (f'{IDENTITY_ENV} is set to {value!r}, which does not look like "Your Name '
                'you@example.com" (a name, then an email). SEC may refuse requests without both.')
    return None


def _writable(directory: Path) -> str | None:
    """Why ``directory`` cannot be written, or None. A directory not there
    yet is asked about at its nearest existing parent, which is where the
    first run creates it. Write and search: a file is created inside it."""
    probe_dir = directory
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    if not probe_dir.is_dir():
        return f"{probe_dir} is not a directory"
    if not os.access(probe_dir, os.W_OK | os.X_OK):
        return f"no write permission on {probe_dir}, or a read-only file system"
    return None


def setup_problems() -> list[str]:
    """Human-readable blockers of a run, in the order to fix them; empty
    when a run can start."""
    problems = []
    identity = _identity_problem()
    if identity is not None:
        problems.append(identity)
    reports = views.reports_dir()
    why = _writable(reports)
    if why is not None:
        problems.append(f"The reports directory {reports} cannot be written ({why}): a run "
                        "would build and then fail to publish.")
    return problems
