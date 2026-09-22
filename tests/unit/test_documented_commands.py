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
from datetime import UTC
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


import journal  # noqa: E402
from app.services.journal.schema_v2 import BeforeBlock, can_lock  # noqa: E402
from app.services.watch.poller import Gate, GateResult  # noqa: E402

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


# ---------------------------------------------------------------------------
# The CLI's own printed hints (round-12)
# ---------------------------------------------------------------------------
# The markdown above is not the surface the operator actually hits. `watch.py
# due` prints a ready-to-copy `journal.py openv2 ...` line, and on print night
# that line is what gets pasted — so it rots the same way the docs did, and
# scanning only markdown let it keep rotting after the docs were fixed.
#
# Placeholders are substituted with concrete values so the hint is checked all
# the way through `can_lock`, not merely through argparse: a hint that parses
# but names an unresolvable window would still strand the operator.

_PLACEHOLDERS = {
    "...": "a thesis long enough to satisfy the validator",
    "<metric>,>,<number>,FY<yyyy>Q<1-4>,,<resolve-by>": "revenue,>,1000000,FY2027Q3,,2026-11-25",
}


def _printed_hints(capsys, monkeypatch) -> list[str]:
    """Drive the real `watch.py due` body down its needs-a-thesis branch and
    harvest every journal invocation it offers the operator."""
    import argparse
    import importlib.util
    from datetime import datetime, timedelta

    spec = importlib.util.spec_from_file_location("watch_cli_hints", ROOT / "scripts" / "watch.py")
    watch_cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch_cli)

    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    watch = watch_cli.wl.Watch(
        ticker="NVDA",
        print_at=now + timedelta(hours=8),
        forms=["10-Q"],
        label="FQ3-27",  # branding, deliberately NOT a valid window
    )
    # `wl` is the shared watchlist module; patch via monkeypatch so the stub
    # is torn down rather than leaking into every later test in the session.
    monkeypatch.setattr(watch_cli.wl, "load", lambda: [watch])
    monkeypatch.setattr(watch_cli, "pinned_thesis_state",
                        lambda w: GateResult(state=Gate.NO_PINNED, detail="no pinned thesis"))

    capsys.readouterr()
    watch_cli.cmd_due(argparse.Namespace(within_hours=36, now=now.isoformat()))
    out = capsys.readouterr().out

    text = out.replace("\\\n", " ")
    return [" ".join(m.group(1).split()) for m in _INVOCATION.finditer(text)]


def test_printed_hint_is_a_command_that_runs(capsys, monkeypatch):
    hints = _printed_hints(capsys, monkeypatch)
    assert hints, "watch.py due printed no journal invocation to check"
    for cmd in hints:
        argv = [_PLACEHOLDERS.get(a, a) for a in _strip_comment(shlex.split(cmd))]
        try:
            parsed = journal.build_parser().parse_args(argv)
        except SystemExit as e:
            pytest.fail(f"watch.py due printed a command that does not parse: "
                        f"journal.py {cmd}\n(argparse exit {e.code})")
        for spec in _assumption_values(argv):
            before = BeforeBlock(
                thesis="a thesis long enough to satisfy the validator",
                conviction=3,
                intended_action=getattr(parsed, "action", "hold"),
                assumptions=[journal._parse_assumption(spec)],
            )
            ok, why = can_lock(before)
            assert ok, f"watch.py due printed an assumption that cannot lock: {spec!r}: {why}"


def test_printed_hint_does_not_offer_the_branding_label_as_a_window():
    # The regression itself: `[FQ3-27]` is the company's fiscal branding and
    # resolves against no period. It must never appear inside an --assumption.
    from app.services.journal.vocabulary import is_wellformed_window
    assert not is_wellformed_window("FQ3-27")
