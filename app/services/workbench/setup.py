"""What stops a first run, said on the home page before the operator types a
ticker rather than as a failed run after it.

Two things, both checked by asking rather than by guessing: the SEC
identity (`sec_client` refuses to fetch without one, and SEC's fair-access
rule wants a name and an email in it), and whether the reports directory
can be written (probed with a file created and removed at once: permission
bits say yes to root and nothing about a read-only mount). A missing `[web]`
extra is not listed: the page could not render without it, and
`scripts/ui.py` says what to install.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from app.services.journal import reporting

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
    """Why ``directory`` cannot be written, or None: probed with a file
    created and removed at once. A directory not there yet is probed at
    its nearest existing parent, which is where the first run creates it."""
    probe_dir = directory
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    if not probe_dir.is_dir():
        return f"{probe_dir} is not a directory"
    try:
        fd, name = tempfile.mkstemp(prefix=".fqe-write-probe-", dir=probe_dir)
    except OSError as e:
        return e.strerror or type(e).__name__
    os.close(fd)
    os.unlink(name)
    return None


def setup_problems() -> list[str]:
    """Human-readable blockers of a run, in the order to fix them; empty
    when a run can start."""
    problems = []
    identity = _identity_problem()
    if identity is not None:
        problems.append(identity)
    reports = reporting.REPORTS
    why = _writable(reports)
    if why is not None:
        problems.append(f"The reports directory {reports} cannot be written ({why}): a run "
                        "would build and then fail to publish.")
    return problems
