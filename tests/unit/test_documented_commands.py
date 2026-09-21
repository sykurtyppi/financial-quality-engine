"""The documented journal commands must actually run.

The entry command is the one step of the protocol a human types under time
pressure, on print night, once per name. It had drifted out of sync with the
parser — `--action` became required and `--assumption` grew to six fields —
and nothing noticed, so the operator's first contact with the journal was an
argparse error. Docs rot silently; this makes them fail loudly instead.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import journal  # noqa: E402
from app.services.journal.schema_v2 import BeforeBlock, can_lock  # noqa: E402

DOCS = [ROOT / "docs" / "earnings_night_runbook.md", ROOT / "journal" / "JOURNAL.md"]

# A fenced invocation, joined across backslash continuations.
_INVOCATION = re.compile(r"^\s*scripts/journal\.py\s+(.+?)(?=\n\s*(?:scripts/|#|```|$))", re.M | re.S)


def _documented_invocations() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for doc in DOCS:
        text = doc.read_text().replace("\\\n", " ")
        for m in _INVOCATION.finditer(text):
            found.append((doc.name, " ".join(m.group(1).split())))
    return found


def test_docs_contain_invocations_to_check():
    # Guards the regex itself: a refactor that stops matching must not turn
    # this file into a silent no-op.
    assert len(_documented_invocations()) >= 4


@pytest.mark.parametrize("doc,cmd", _documented_invocations())
def test_documented_command_parses(doc, cmd):
    argv = _strip_comment(shlex.split(cmd))
    try:
        journal.build_parser().parse_args(argv)
    except SystemExit as e:  # argparse exits 2 on a bad invocation
        pytest.fail(f"{doc}: documented command does not parse: journal.py {cmd}\n(argparse exit {e.code})")


def test_documented_assumption_rows_are_lockable():
    # A row that parses but cannot satisfy the specificity floor would send
    # the operator round the loop again at the lock step.
    rows = [
        a for _doc, cmd in _documented_invocations()
        for a in _assumption_values(_strip_comment(shlex.split(cmd)))
    ]
    assert rows, "no --assumption documented; the lock floor requires one"
    for spec in rows:
        before = BeforeBlock(
            thesis="a thesis long enough to satisfy the validator",
            conviction=3,
            intended_action="hold",
            assumptions=[journal._parse_assumption(spec)],
        )
        ok, why = can_lock(before)
        assert ok, f"documented assumption {spec!r} cannot lock: {why}"


def _strip_comment(argv: list[str]) -> list[str]:
    """Drop a trailing shell comment; docs annotate commands inline."""
    for i, a in enumerate(argv):
        if a.startswith("#"):
            return argv[:i]
    return argv


def _assumption_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--assumption" and i + 1 < len(argv)]
