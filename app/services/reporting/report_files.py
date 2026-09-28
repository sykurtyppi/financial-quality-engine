"""The files a report run leaves on disk, and how a rerun treats them.

A report is read at ``reports/<T>_<day>.md``, with its evidence ledger
(``.ledger.json``) and, once audited, ``<stem>_audit.md`` beside it. A second
run on the same day writes the same names, so before this module it replaced
the first report with no copy (Hermes audit round 7, finding 3: rollback) and
left the first run's audit beside the second run's report, where
``audit_for`` would pair them.

Each run is one GENERATION, kept whole and never modified once published
(Hermes deep audits, findings 1-3 and re-audit F1-F3):

- a rebuild is built in ``<dir>/.staging/<id>/``; a build that fails, or
  one without its evidence ledger, publishes nothing;
- a finished rebuild is stamped with its ``generation_id`` (the report's
  last line, the ledger's field), fsynced, and renamed whole into
  ``<dir>/.generations/<base>/<stamp>_<seq>_<id>/``, read-only;
- ONE pointer, the symlink ``.generations/<base>/current``, is then swapped
  atomically to it. The live names are fixed symlinks through that pointer
  (``<base>.md`` -> ``.generations/<base>/current/<base>.md``; the audit's
  only while the live run has one), so the report, ledger and audit a reader
  opens always come from one generation:
  a process killed at any point leaves the earlier generation live or the
  new one, never one's report beside the other's ledger. Two rebuilds
  publish one after the other (``publish_lock``);
- earlier generations stay where they are: they are the archive, and
  ``restore`` points the live names back at one.

Readers that must see one run whole resolve the pointer once (``read_live``)
and read from that generation's own directory, which no publish ever
touches again.
"""

from __future__ import annotations

import errno
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

STAGING_DIR = ".staging"
GENERATIONS_DIR = ".generations"
CURRENT = "current"
REPLAY_SUFFIX = ".replay.md"
# How a report and its audit state their generation (the ledger carries it as
# the `generation_id` field): one line, so a reader never needs to parse more.
GENERATION_LINE = "- Generation: "
_GENERATION_RE = re.compile(r"^(?:- Generation: |<!-- generation: )([0-9a-f]{32})\b", re.M)
_ROLES = ("report", "ledger", "audit")
# A published file is never written again: read-only, so a write to a live
# name (a hand `cp` over it) fails instead of editing a kept generation.
READ_ONLY = 0o444


class NotPublished(RuntimeError):
    """A rebuild that could not be published whole: the earlier run, if any,
    is still live and unchanged."""


def _base(report: Path) -> str:
    """`AAPL_2026-09-26.md` -> `AAPL_2026-09-26`; a replay keeps `.replay`."""
    return report.name.removesuffix(".md")


def _names(base: str) -> dict[str, str]:
    """The file names of a run by role; they follow `ledger_path` and
    `earnings_brief.audit_for`."""
    return {"report": f"{base}.md", "ledger": f"{base}.ledger.json", "audit": f"{base}_audit.md"}


def _companions(report: Path) -> dict[str, Path]:
    """The live names of `report`'s files by role."""
    return {role: report.with_name(name) for role, name in _names(_base(report)).items()}


def _home(report: Path) -> Path:
    return report.parent / GENERATIONS_DIR / _base(report)


def _pointer(report: Path) -> Path:
    return _home(report) / CURRENT


def live_name(path: Path) -> Path:
    """The live name of a report, given it or one of its generations' paths
    (``<dir>/.generations/<base>/<gen>/<base>.md`` -> ``<dir>/<base>.md``)."""
    home = path.parent.parent
    if home.parent.name == GENERATIONS_DIR and home.name == _base(path):
        return home.parent.parent / path.name
    return path


def current_generation(report: Path) -> Path | None:
    """The generation directory ``report``'s live names resolve to, or None
    when none is live (never published, set aside, or from before
    generations). Only a missing pointer reads as none: any other failure to
    read it (permission, I/O) raises rather than being taken for "nothing
    published" (Hermes re-audit F2)."""
    pointer = _pointer(report)
    try:
        target = os.readlink(pointer)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.EINVAL:  # a directory, not a link: copied dereferenced
            raise OSError(e.errno, f"{pointer} is not a symlink: the reports directory was "
                          "copied with its links dereferenced; copy it with `cp -a`") from e
        raise
    return _home(report) / target


def _seq(gen: Path) -> int:
    """A generation's publish order, from its name (``<stamp>_<seq>_<id>``)."""
    parts = gen.name.split("_")
    return int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else -1


def generations(report: Path) -> list[Path]:
    """Every whole generation of ``report`` kept on disk, in publish order.
    Not the pointer, not a hidden directory (a build or copy interrupted
    part way), and not a directory holding no report."""
    home = _home(report)
    if not home.is_dir():
        return []
    return sorted(
        (d for d in home.iterdir()
         if d.is_dir() and not d.is_symlink() and d.name != CURRENT
         and not d.name.startswith(".") and (d / report.name).is_file()),
        key=lambda d: (_seq(d), d.name))


def _name_next(report: Path, stamp: str, tag: str) -> str:
    """``<stamp>_<seq>_<tag>``: the sequence number orders generations by
    publish even within one second (the first rebuild after this change
    publishes a kept run and a new one in the same call)."""
    home = _home(report)
    taken = [_seq(d) for d in home.iterdir() if d.is_dir()] if home.is_dir() else []
    return f"{stamp}_{max(taken, default=0) + 1:04d}_{tag}"


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


def _fsync(path: Path) -> None:
    """Make ``path`` (a file, or a directory's entries) durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _symlink(link: Path, target: str) -> None:
    """Point ``link`` at ``target`` atomically (a new link renamed over it):
    a reader resolves the old target or the new one, never neither."""
    if link.is_symlink() and os.readlink(link) == target:
        return
    tmp = link.with_name(f".{link.name}.{uuid.uuid4().hex[:8]}.link")
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def publish_lock(report: Path) -> Iterator[None]:
    """The lock every change to ``report``'s live names holds, across
    processes: publishes, ``set_aside`` and ``restore`` happen one after the
    other. Readers take no lock: a generation, once published, never
    changes. It is a sidecar in the staging directory; ``flock`` is
    advisory and unreliable over NFS, so the reports directory must be
    local, as the watchlist's lock already assumes."""
    lock = report.parent / STAGING_DIR / f"{_base(report)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


@dataclass
class Staged:
    """Where a rebuild writes before it is published: ``report`` must be
    written by the caller and ``ledger`` by the builder, or nothing is
    published; the publish stamps both with ``generation_id``. ``archived``
    lists the earlier generation's files once the rebuild is live (they
    stay where they are)."""

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
    write_atomic(staged.ledger, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    if m is None:
        # Appended in place (the staged file is this rebuild's own), never
        # rewritten: what the builder wrote is the report, byte for byte, up
        # to the stamp.
        with staged.report.open("a", newline="") as fh:
            fh.write(f"\n\n{GENERATION_LINE}{gid} "
                     "(this report, its evidence ledger and its audit carry the same id)\n")
    for path in (staged.report, staged.ledger):
        os.chmod(path, READ_ONLY)
        _fsync(path)
    _fsync(staged.report.parent)


def write_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` through a temporary file beside it,
    fsynced, and ``os.replace``: a reader sees the old file or the new one,
    and a failed write leaves no temporary behind. ``mode``, when given, is
    set before the file takes its name."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


@contextmanager
def replacing(report: Path, *, now: datetime | None = None) -> Iterator[Staged]:
    """Build a report's replacement off to the side, then publish it.

    Inside the block the caller builds into ``staged.report`` and
    ``staged.ledger`` (in ``<dir>/.staging/<id>/``, which no live-report glob
    reads). If the block raises, or returns without both (or with one naming
    another generation, ``NotPublished``), the staged directory is removed
    and nothing live changes. Otherwise, holding ``publish_lock``, the
    stamped generation is renamed whole into ``.generations/<base>/`` and the
    one pointer swapped to it (the module docstring). Every step before the
    swap leaves the earlier generation live; the swap is one rename.

    The staging directory is shared by every rebuild writing to ``<dir>`` and
    is never removed: a rebuild that removed it once it looked empty pulled
    it from under another still building there (round-9 audit F1).
    """
    gid = uuid.uuid4().hex
    work = report.parent / STAGING_DIR / gid
    work.mkdir(parents=True)
    names = _names(_base(report))
    staged = Staged(report=work / names["report"], ledger=work / names["ledger"],
                    generation_id=gid)
    try:
        yield staged
        _seal(staged, report.name)
        with publish_lock(report):
            _publish(report, staged, work, now)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # gone already once published


def _publish(report: Path, staged: Staged, work: Path, now: datetime | None) -> None:
    """The publish; the caller holds ``publish_lock``."""
    home = _home(report)
    home.mkdir(parents=True, exist_ok=True)
    _adopt(report, now)
    previous = current_generation(report)
    gen = home / _name_next(report, _stamp(now), staged.generation_id)
    os.rename(work, gen)
    _fsync(home)
    _link_live_names(report)
    _symlink(_pointer(report), gen.name)  # the switch: one rename
    _fsync(home)
    link_audit(report)  # the new run has none yet; before this, the name resolved to nothing
    if previous is not None:
        staged.archived.extend(
            p for name in _names(_base(report)).values() if (p := previous / name).exists())


def _link_live_names(report: Path) -> None:
    """The fixed live names of the report and ledger: each a symlink through
    the pointer. Created before the pointer first exists, they resolve to
    nothing until then."""
    base = _base(report)
    names = _names(base)
    for role in ("report", "ledger"):
        _symlink(report.with_name(names[role]), f"{GENERATIONS_DIR}/{base}/{CURRENT}/{names[role]}")
    _fsync(report.parent)


def link_audit(report: Path) -> None:
    """The audit's live name, present only while the live generation has an
    audit: through the pointer like the others, so it never names another
    run's audit, and absent otherwise so a listing of the reports directory
    shows no audit that is not there. The caller holds ``publish_lock``."""
    base = _base(report)
    name = _names(base)["audit"]
    link = report.with_name(name)
    gen = current_generation(report)
    if gen is not None and (gen / name).is_file():
        _symlink(link, f"{GENERATIONS_DIR}/{base}/{CURRENT}/{name}")
    elif link.is_symlink():
        link.unlink()
    _fsync(report.parent)


def _adopt(report: Path, now: datetime | None) -> None:
    """Live files from before generations (plain files at the live names)
    become a generation of their own, so a rebuild keeps them as it keeps
    any earlier run. Copied into a new generation, which the pointer names
    before any plain file is replaced by its symlink, so every step shows
    the same run. A crash part way leaves plain files identical to the
    generation the pointer names; the next publish finishes the job."""
    live = _companions(report)
    plain = {role: p for role, p in live.items() if p.exists() and not p.is_symlink()}
    if not plain:
        return
    gen = current_generation(report)
    if gen is None:
        # A crash after the copy but before the pointer left the copy whole:
        # it is reused, not copied again.
        same = [d for d in generations(report) if d.name.endswith("_adopted") and all(
            (d / p.name).is_file() and (d / p.name).read_bytes() == p.read_bytes()
            for p in plain.values())]
        gen = same[-1] if same else _copy_adopted(report, plain)
        _symlink(_pointer(report), gen.name)
        _fsync(gen.parent)
    for role, p in plain.items():
        kept = gen / p.name
        if not (kept.is_file() and kept.read_bytes() == p.read_bytes()):
            raise NotPublished(
                f"{p.name} is a plain file that is not the live generation's {role}; "
                "set it aside by hand before rebuilding")
    _link_live_names(report)
    link_audit(report)


def _copy_adopted(report: Path, plain: dict[str, Path]) -> Path:
    """The plain files, copied whole into a new generation named for when
    they were written; a copy that fails leaves nothing behind."""
    written = max(p.stat().st_mtime for p in plain.values())
    stamp = datetime.fromtimestamp(written, UTC).strftime("%Y%m%dT%H%M%SZ")
    gen = _home(report) / _name_next(report, stamp, "adopted")
    tmp = gen.with_name(f".{gen.name}.{uuid.uuid4().hex[:8]}")
    tmp.mkdir()
    try:
        for p in plain.values():
            shutil.copy2(p, tmp / p.name)
            os.chmod(tmp / p.name, READ_ONLY)
            _fsync(tmp / p.name)
        _fsync(tmp)
        os.rename(tmp, gen)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _fsync(gen.parent)
    return gen


def set_aside(report: Path, *, now: datetime | None = None) -> list[Path]:
    """Take the live run off the live names (they then resolve to nothing)
    and return its files, which stay in their generation. For setting a run
    aside by hand; a rebuild or ``restore`` replaces the live run in one
    step instead."""
    with publish_lock(report):
        if _home(report).is_dir() or any(p.exists() for p in _companions(report).values()):
            _home(report).mkdir(parents=True, exist_ok=True)
            _adopt(report, now)
        gen = current_generation(report)
        if gen is None:
            return []
        _pointer(report).unlink()
        _fsync(_home(report))
        link_audit(report)
        return [p for name in _names(_base(report)).values() if (p := gen / name).exists()]


def restore(report: Path, generation: str) -> Path:
    """Make a kept generation live again, in one step: ``generation`` is its
    directory name or a unique part of it (its id, its stamp). Returns the
    restored report's path in its generation. A plain file at a live name
    that is not the live run's stops it, as it stops a rebuild."""
    with publish_lock(report):
        _home(report).mkdir(parents=True, exist_ok=True)
        _adopt(report, None)
        kept = generations(report)
        matches = ([d for d in kept if d.name == generation]
                   or [d for d in kept if generation in d.name])
        if len(matches) != 1:
            raise ValueError(f"{report.name}: {len(matches)} generations match {generation!r}")
        (gen,) = matches
        _link_live_names(report)
        _symlink(_pointer(report), gen.name)
        _fsync(gen.parent)
        link_audit(report)
        return gen / report.name


def is_live_report(path: Path) -> bool:
    """A report a reader would call "the latest": not an audit written beside
    one, and not a historical replay (``.replay.md``), which rebuilds an old
    day with today's code and is never the current view."""
    return not path.stem.endswith("_audit") and not path.name.endswith(REPLAY_SUFFIX)


@dataclass(frozen=True)
class LiveRun:
    """One live run, pinned: ``report``, ``ledger`` and ``audit`` are its
    generation's own paths, which no later publish changes, so they can be
    read (or handed to another process) after this returns. A ledger or
    audit that names another generation is in ``stale``, never returned as
    the run's own. ``generation_dir`` is None for files from before
    generations, read at their live names."""

    text: str
    generation_id: str | None
    report: Path
    ledger: Path | None
    audit: Path | None
    stale: tuple[Path, ...] = ()
    generation_dir: Path | None = None


def read_live(report: Path) -> LiveRun | None:
    """The live run of ``report``, pinned to one generation: the pointer is
    resolved once and every file is read from that generation. None when no
    run is live. Given a generation's own path, that generation is read
    (live or not). Files from before generations are read at their live
    names, and a report and ledger that both name no generation still pair."""
    gen = report.parent if live_name(report) != report else current_generation(report)
    if gen is None and report.is_symlink():
        return None  # the live names exist but name no run (set aside)
    paths = (_companions(report) if gen is None
             else {role: gen / name for role, name in _names(_base(report)).items()})
    try:
        text = paths["report"].read_text()
    except FileNotFoundError:
        return None
    m = _GENERATION_RE.search(text)
    gid = m.group(1) if m else None
    own: dict[str, Path | None] = {"ledger": None, "audit": None}
    stale = []
    for role in own:
        path = paths[role]
        if not path.exists():
            continue
        if generation_of(path) == gid:
            own[role] = path
        else:
            stale.append(path)
    return LiveRun(text, gid, paths["report"], own["ledger"], own["audit"], tuple(stale), gen)
