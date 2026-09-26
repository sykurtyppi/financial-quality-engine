"""The files a report run leaves on disk, and how a rerun treats them.

A report is written to ``reports/<T>_<day>.md`` with its evidence ledger
(``.ledger.json``) and, once audited, ``<stem>_audit.md`` beside it. A second
run on the same day writes the same paths, so before this module it replaced
the first report with no copy (Hermes audit round 7, finding 3: rollback) and
left the first run's audit beside the second run's report, where
``audit_for`` would pair them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

ARCHIVE_DIR = "archive"
REPLAY_SUFFIX = ".replay.md"


def _base(report: Path) -> str:
    """`AAPL_2026-09-26.md` -> `AAPL_2026-09-26`; a replay keeps `.replay`."""
    return report.name.removesuffix(".md")


def _companions(report: Path) -> dict[str, Path]:
    """The report's files by role; the names follow `ledger_path` and
    `earnings_brief.audit_for`."""
    base = _base(report)
    return {
        "report": report,
        "ledger": report.with_name(f"{base}.ledger.json"),
        "audit": report.with_name(f"{base}_audit.md"),
    }


def archive_existing(report: Path, *, now: datetime | None = None) -> list[Path]:
    """Move whatever an earlier run left at ``report``'s paths into
    ``<dir>/archive/`` and return where each file went (empty when nothing
    was there).

    Everything moves together, so the archived report keeps its own ledger
    and audit (``<base>.<HHMMSS>.md`` / ``.ledger.json`` / ``_audit.md``), and
    nothing from the earlier run is left to sit beside the new report. A
    stamp already taken (two runs within one second) gets a ``-<n>`` suffix;
    nothing in the archive is ever overwritten, and when 100 stamps of one
    second are taken nothing moves (``FileExistsError``).
    """
    present = {role: p for role, p in _companions(report).items() if p.exists()}
    if not present:
        return []
    archive = report.parent / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%H%M%S")
    base = _base(report)
    for n in range(100):  # bounded: a defect here fails, it does not spin
        tag = stamp if n == 0 else f"{stamp}-{n}"
        target = _companions(archive / f"{base}.{tag}.md")
        if not any(p.exists() for p in target.values()):
            break
    else:
        raise FileExistsError(
            f"{archive}: 100 runs of {base} already archived at {stamp}; nothing moved")
    moved = []
    for role, src in present.items():
        src.replace(target[role])
        moved.append(target[role])
    return moved


def is_live_report(path: Path) -> bool:
    """A report a reader would call "the latest": not an audit written beside
    one, and not a historical replay (``.replay.md``), which rebuilds an old
    day with today's code and is never the current view."""
    return not path.stem.endswith("_audit") and not path.name.endswith(REPLAY_SUFFIX)
