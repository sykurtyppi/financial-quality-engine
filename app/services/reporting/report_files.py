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

One run is one GENERATION (Hermes deep audit, findings 1-2): the report, its
ledger and its audit carry the same ``generation_id``, a publish requires the
staged report and ledger to carry the rebuild's own id (a run whose ledger
could not be built publishes nothing: the earlier complete run stays live),
and every publish of one report takes the same cross-process lock, so two
rebuilds racing on a filing night cannot leave one's report beside the
other's ledger. ``read_live`` reads a report with the files that belong to
it, under the same lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
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
# How a report and its audit state their generation (the ledger carries it as
# the `generation_id` field): one line, so a reader never needs to parse more.
GENERATION_LINE = "- Generation: "
_GENERATION_RE = re.compile(r"^(?:- Generation: |<!-- generation: )([0-9a-f]{32})\b", re.M)


class NotPublished(RuntimeError):
    """A rebuild that could not be published whole: the earlier run, if any,
    is still live and unchanged."""


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
    with publish_lock(report):
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


def generation_of(path: Path) -> str | None:
    """The generation a report, ledger or audit file names, or None (a file
    written before generations, or one that is missing or unreadable)."""
    try:
        text = path.read_text()
    except OSError:
        return None
    if path.name.endswith(".ledger.json"):
        try:
            gid = json.loads(text).get("generation_id")
        except (ValueError, AttributeError):
            return None
        return gid if isinstance(gid, str) else None
    m = _GENERATION_RE.search(text)
    return m.group(1) if m else None


@contextmanager
def publish_lock(report: Path, *, shared: bool = False) -> Iterator[None]:
    """The lock every publish of ``report`` (and of its audit) holds, across
    processes; a reader takes it shared. It is a sidecar in the staging
    directory, not the report itself: ``os.replace`` swaps the report's
    inode, and a lock on the old one would guard nothing. ``flock`` is
    advisory and unreliable over NFS, so the reports directory must be
    local, as the watchlist's lock already assumes."""
    staging = report.parent / STAGING_DIR
    try:
        staging.mkdir(parents=True, exist_ok=True)
        fd = os.open(staging / f"{_base(report)}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        if not shared:
            raise
        # A reader of a directory it cannot write (a copied-out report) has
        # no publisher to exclude.
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


@dataclass
class Staged:
    """Where a rebuild writes before it is published: ``report`` must be
    written by the caller and ``ledger`` by the builder, or nothing is
    published; the publish stamps both with ``generation_id``. ``archived``
    lists where the earlier run went once the rebuild is published."""

    report: Path
    ledger: Path
    generation_id: str
    archived: list[Path] = field(default_factory=list)


def _seal(staged: Staged, name: str) -> None:
    """A publish is one whole generation or nothing: both files must exist,
    and each is stamped with the rebuild's id (the report on its last line,
    the ledger in `generation_id`). A file already naming another generation
    was not written by this rebuild."""
    if not staged.report.is_file():
        raise NotPublished(f"{name}: the rebuild wrote no report; nothing published")
    if not staged.ledger.is_file():
        raise NotPublished(f"{name}: the rebuild wrote no evidence ledger; nothing published, "
                           "the earlier run stays live")
    gid = staged.generation_id
    try:
        doc = json.loads(staged.ledger.read_text())
    except ValueError as e:
        raise NotPublished(f"{name}: the evidence ledger is not valid JSON ({e}); "
                           "nothing published") from e
    if not isinstance(doc, dict):
        raise NotPublished(f"{name}: the evidence ledger is not a JSON object; nothing published")
    text = staged.report.read_text()
    m = _GENERATION_RE.search(text)
    for path, found in ((staged.ledger, doc.get("generation_id")),
                        (staged.report, m.group(1) if m else None)):
        if found not in (None, gid):
            raise NotPublished(f"{name}: {path.name} names generation {found}, not this "
                               f"rebuild's {gid}; nothing published")
    doc["generation_id"] = gid
    _write_atomic(staged.ledger, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    if m is None:
        # Appended, never rewritten into the text: what the builder wrote is
        # the report, byte for byte, up to the stamp.
        _write_atomic(staged.report, text + f"\n\n{GENERATION_LINE}{gid} "
                      "(this report, its evidence ledger and its audit carry the same id)\n")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def replacing(report: Path, *, now: datetime | None = None) -> Iterator[Staged]:
    """Build a report's replacement off to the side, then publish it.

    Inside the block the caller builds into ``staged.report`` and
    ``staged.ledger`` (under ``<dir>/.staging/``, which no live-report glob
    reads). If the block raises, the staged files are removed and the live
    report, ledger and audit are untouched. If it returns without writing
    ``staged.report`` and ``staged.ledger`` (or one names another
    generation), nothing is published (``NotPublished``) and the live run is
    untouched. Otherwise both are stamped with ``staged.generation_id`` and,
    holding ``publish_lock``:

    1. the earlier run's report, ledger and audit are COPIED to the archive
       (one stamp, as ``archive_existing`` names them); a copy that fails
       removes the copies already made, so no partial archived run is left;
    2. the earlier run's audit, now archived, is removed from beside it;
    3. the new ledger replaces the live one (``os.replace``);
    4. the new report replaces the live one (``os.replace``): the live path
       is never missing.

    If anything in 2-4 fails (an OSError, an interrupt) before the report is
    published, the earlier run's ledger and audit are put back from their
    archive copies (or the new ledger removed, on a first run) and those
    copies deleted: the live run is exactly as it was, not a mismatched pair
    a later rebuild would archive as one run (round-9 review R1-R3). Report
    and ledger are two files, so between steps 3 and 4 the new ledger sits
    beside the earlier report for an instant; the report is never absent, and
    a reader holding the lock (``read_live``) never sees that instant. Two
    rebuilds publish one after the other, never interleaved: the last one's
    report and ledger are live together.

    The staging directory is shared by every rebuild writing to ``<dir>`` and
    is never removed: a rebuild that removed it once it looked empty pulled
    it from under another still building there (round-9 audit F1).
    """
    staging = report.parent / STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = Staged(report=staging / f"{token}.md", ledger=staging / f"{token}.ledger.json",
                    generation_id=token)
    try:
        yield staged
        _seal(staged, report.name)
        with publish_lock(report):
            _publish(report, staged, now)
    finally:
        staged.report.unlink(missing_ok=True)
        staged.ledger.unlink(missing_ok=True)


def _publish(report: Path, staged: Staged, now: datetime | None) -> None:
    """Steps 1-4 of ``replacing``; the caller holds ``publish_lock``."""
    live = _companions(report)
    present = {role: p for role, p in live.items() if p.exists()}
    target: dict[str, Path] = {}
    if present:
        (report.parent / ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
        target = _archive_targets(report, now)
        try:
            for role, src in present.items():
                shutil.copy2(src, target[role])
        except BaseException:
            _discard(target[role] for role in present)  # the stamp was free: ours
            raise
    published = False
    try:
        live["audit"].unlink(missing_ok=True)
        os.replace(staged.ledger, live["ledger"])
        os.replace(staged.report, live["report"])
        published = True
    except BaseException:
        if not published:
            _put_back(live, present, target)
        raise
    staged.archived.extend(target[role] for role in present)


def _discard(paths) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _put_back(live: dict[str, Path], present: dict[str, Path], target: dict[str, Path]) -> None:
    """Undo a publish that failed before the report went live: the earlier
    run's ledger and audit return from their archive copies (atomically), a
    first run's new ledger is removed, and the archive copies are deleted —
    the earlier run is live again, so it is not also archived."""
    for role in ("ledger", "audit"):
        if role in present:
            tmp = live[role].with_name(f".{live[role].name}.{uuid.uuid4().hex[:8]}.restore")
            shutil.copy2(target[role], tmp)
            os.replace(tmp, live[role])
        elif role == "ledger":
            live[role].unlink(missing_ok=True)
    _discard(target[role] for role in present)


def is_live_report(path: Path) -> bool:
    """A report a reader would call "the latest": not an audit written beside
    one, and not a historical replay (``.replay.md``), which rebuilds an old
    day with today's code and is never the current view."""
    return not path.stem.endswith("_audit") and not path.name.endswith(REPLAY_SUFFIX)


@dataclass(frozen=True)
class LiveRun:
    """A live report with the files that belong to its generation. A ledger
    or audit beside it that names another generation (an audit finished
    after a rebuild, a file copied back by hand) is in ``stale``, never
    returned as the report's own."""

    text: str
    generation_id: str | None
    ledger: Path | None
    audit: Path | None
    stale: tuple[Path, ...] = ()


def read_live(report: Path) -> LiveRun | None:
    """The live report and its own ledger and audit, read under the shared
    publish lock (no rebuild is half-published while it reads). None when
    there is no report. Files from before generations name none: a report
    and a ledger that both name none still pair, as they always did."""
    with publish_lock(report, shared=True):
        try:
            text = report.read_text()
        except FileNotFoundError:
            return None
        m = _GENERATION_RE.search(text)
        gid = m.group(1) if m else None
        own: dict[str, Path | None] = {"ledger": None, "audit": None}
        stale = []
        for role in own:
            path = _companions(report)[role]
            if not path.exists():
                continue
            if generation_of(path) == gid:
                own[role] = path
            else:
                stale.append(path)
        return LiveRun(text, gid, own["ledger"], own["audit"], tuple(stale))

