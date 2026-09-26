"""The files a report run leaves on disk, and how a rerun treats them.

A report is written to ``reports/<T>_<day>.md`` with its evidence ledger
(``.ledger.json``) and, once audited, ``<stem>_audit.md`` beside it. A second
run on the same day writes the same paths, so before this module it replaced
the first report with no copy (Hermes audit round 7, finding 3: rollback) and
left the first run's audit beside the second run's report, where
``audit_for`` would pair them.

A rerun now goes through ``replacing``: the new report and ledger are built
in a staging directory, and the earlier run is archived and replaced only
once they exist. A build that fails leaves the live report, ledger and audit
exactly as they were (Hermes audit round 8, finding 2: archiving first left
no live report behind a failed rebuild).
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ARCHIVE_DIR = "archive"
STAGING_DIR = ".staging"
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


def _archive_targets(report: Path, now: datetime | None) -> dict[str, Path]:
    """Free archive paths for `report`'s files, all under one stamp
    (``<base>.<HHMMSS>[-n].*``). A stamp any companion already holds is
    taken; after 100 stamps within one second nothing is archived."""
    archive = report.parent / ARCHIVE_DIR
    stamp = (now or datetime.now(UTC)).strftime("%H%M%S")
    base = _base(report)
    for n in range(100):  # bounded: a defect here fails, it does not spin
        tag = stamp if n == 0 else f"{stamp}-{n}"
        target = _companions(archive / f"{base}.{tag}.md")
        if not any(p.exists() for p in target.values()):
            return target
    raise FileExistsError(
        f"{archive}: 100 runs of {base} already archived at {stamp}; nothing moved")


def archive_existing(report: Path, *, now: datetime | None = None) -> list[Path]:
    """Move whatever an earlier run left at ``report``'s paths into
    ``<dir>/archive/`` and return where each file went (empty when nothing
    was there). For setting a run aside by hand (the restore procedure); a
    rebuild goes through ``replacing``, which archives only once the new
    report exists.

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
    (report.parent / ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    target = _archive_targets(report, now)
    moved = []
    for role, src in present.items():
        src.replace(target[role])
        moved.append(target[role])
    return moved


@dataclass
class Staged:
    """Where a rebuild writes before it is published: ``report`` must be
    written by the caller; ``ledger`` is handed to the builder (which may not
    write it — a ledger failure is logged, not fatal). ``archived`` lists
    where the earlier run went once the rebuild is published."""

    report: Path
    ledger: Path
    archived: list[Path] = field(default_factory=list)


@contextmanager
def replacing(report: Path, *, now: datetime | None = None) -> Iterator[Staged]:
    """Build a report's replacement off to the side, then publish it.

    Inside the block the caller builds into ``staged.report`` and
    ``staged.ledger`` (under ``<dir>/.staging/``, which no live-report glob
    reads). If the block raises, the staged files are removed and the live
    report, ledger and audit are untouched. If it returns without writing
    ``staged.report``, nothing is published (``RuntimeError``). Otherwise:

    1. the earlier run's report, ledger and audit are COPIED to the archive
       (one stamp, as ``archive_existing`` names them);
    2. the new ledger replaces the live one (``os.replace``), or the live
       ledger is removed when the build wrote none, so a stale ledger never
       sits beside the new report;
    3. the new report replaces the live one (``os.replace``): the live path
       is never missing;
    4. the earlier run's audit, now archived, is removed from beside it.

    Report and ledger are two files, so between steps 2 and 3 the new ledger
    sits beside the earlier report for an instant; the report is never absent.

    The staging directory is shared by every rebuild writing to ``<dir>`` and
    is never removed: a rebuild that removed it once it looked empty pulled
    it from under another still building there (round-9 audit F1).
    """
    staging = report.parent / STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staged = Staged(report=staging / f"{token}.md", ledger=staging / f"{token}.ledger.json")
    try:
        yield staged
        if not staged.report.is_file():
            raise RuntimeError(f"{report.name}: the rebuild wrote no report; nothing published")
        live = _companions(report)
        present = {role: p for role, p in live.items() if p.exists()}
        if present:
            (report.parent / ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
            target = _archive_targets(report, now)
            for role, src in present.items():
                shutil.copy2(src, target[role])
                staged.archived.append(target[role])
        if staged.ledger.is_file():
            os.replace(staged.ledger, live["ledger"])
        else:
            live["ledger"].unlink(missing_ok=True)
        os.replace(staged.report, live["report"])
        live["audit"].unlink(missing_ok=True)
    finally:
        staged.report.unlink(missing_ok=True)
        staged.ledger.unlink(missing_ok=True)


def is_live_report(path: Path) -> bool:
    """A report a reader would call "the latest": not an audit written beside
    one, and not a historical replay (``.replay.md``), which rebuilds an old
    day with today's code and is never the current view."""
    return not path.stem.endswith("_audit") and not path.name.endswith(REPLAY_SUFFIX)
