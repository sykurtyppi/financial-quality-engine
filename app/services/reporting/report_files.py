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
  last line, the ledger's field) and with the engine commit that built it
  (``engine_commit``: the line above, the ledger's ``engine_commit``),
  fsynced, and renamed whole into
  ``<dir>/.generations/<base>/<stamp>_<seq>_<id>/``, read-only;
- ONE pointer, the symlink ``.generations/<base>/current``, is then swapped
  atomically to it. The live names are fixed symlinks through that pointer
  (``<base>.md`` -> ``.generations/<base>/current/<base>.md``; the audit's
  only while the live run has one), so the report, ledger and audit a reader
  opens always come from one generation:
  a process killed at any point leaves the earlier generation live or the
  new one, never one's report beside the other's ledger. Two rebuilds
  publish one after the other (``publish_lock``);
- a publish that RAISES leaves the earlier generation live (Hermes audit of
  424b0b4, finding 3a): every step that can fail runs before the pointer's
  rename, and a failure after it (the directory's fsync) puts the earlier
  pointer back, reads it back, and only then says so (``NotPublished``). A
  put-back that fails says the new generation may be live
  (``PublishInDoubt``), never that the earlier one is;
- earlier generations stay where they are: they are the archive, and
  ``restore`` points the live names back at one;
- a FENCED rebuild (the workbench's: a number taken when its run was asked
  for, `workbench.fencing`) is sealed with its fence and, under the
  publish lock and the ticker's lock, compared before the switch with the
  ticker's high-water mark of published fences (every day's report, and
  across a restore) and with the live generation's own: a lower one is kept
  in the archive, marked superseded, and never made live (``Superseded``;
  Hermes audit of PR #118, finding 1, and the review of 2cbba1c). A fence
  that cannot be read fails the publish closed. An unfenced rebuild
  publishes as before.

Readers that must see one run whole resolve the pointer once (``read_live``)
and read from that generation's own directory, which no publish ever
touches again.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
# Which code built a published run: a season is run from a pinned commit, and
# a report that cannot say which one cannot be traced back to it.
ENGINE_LINE = "- Engine: "
# For a deployment without a git checkout (or a copy of one, as the drill's
# workspaces are): the commit it was copied from, stated by whoever copied it.
ENGINE_ENV = "FQE_ENGINE_COMMIT"
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
# The engine is its code. Data files it rewrites itself (journal/watchlist.json
# on every re-arm) are not, so they never make a checkout read as modified.
_ENGINE_PATHS = ("app", "scripts", "pyproject.toml")


class NotPublished(RuntimeError):
    """A rebuild that could not be published whole (or a ``restore`` or
    ``set_aside`` that did not happen): the earlier run, if any, is still
    live and unchanged."""


class PublishInDoubt(RuntimeError):
    """A change to the live names that failed after the pointer was switched,
    and whose switch back failed too: the NEW generation may be live. Not a
    `NotPublished`, whose handlers tell the operator the earlier run is. Its
    message says how to check which run is live and how to put the earlier
    one back; every command that publishes prints it as it is and exits
    `PUBLISH_IN_DOUBT_RC`, and never stamps a journal entry ``reported``
    after one."""


# The exit code of every command that publishes a report (generate_report.py,
# journal.py report, watch.py poll/sweep) when a publish ended in doubt: not 1,
# which says a run failed and nothing changed. It is outside every other code
# those commands use, so an alert can key on it.
PUBLISH_IN_DOUBT_RC = 8
# `journal.py report` when its report WAS published and the entry could not
# be stamped `reported` (Hermes re-audit of 84e65b0, finding 4): not 1 (nothing
# published), not 8 (which run is live is unknown). The run is known and
# named, with the `mark-reported --generation` command that stamps it.
PUBLISHED_NOT_STAMPED_RC = 9


class Superseded(RuntimeError):
    """A fenced rebuild whose fence is below one already published for the
    ticker (`Fence.mark`) or below the live generation's: a run asked for
    later has published, and this one, finished after it, must not replace
    it (Hermes audit of PR #118, finding 1: an abandoned workbench run ended
    last and took the live names from the run that replaced it). Not a
    failure, and not `NotPublished`: the generation is whole and kept in the
    archive, marked superseded (``generation``, ``generation_id``), the live
    run is unchanged (``live_generation_id``: this report's, None when this
    day's report has none), and nothing is in doubt."""

    def __init__(self, report: Path, generation: Path, generation_id: str, fence: int,
                 live_generation_id: str | None, live_fence: int) -> None:
        live = ("" if live_generation_id is None
                else f"; generation {live_generation_id} stays live")
        super().__init__(
            f"{report.name}: superseded: this run (request {fence}) finished after a run asked "
            f"for later (request {live_fence}) had published{live}; this one is kept as "
            f"{generation.name}, not live")
        self.generation = generation
        self.generation_id = generation_id
        self.fence = fence
        self.live_generation_id = live_generation_id
        self.live_fence = live_fence


@dataclass(frozen=True)
class Fence:
    """A run's place in its ticker's order of requests (`workbench.fencing`
    takes ``number`` when the run is asked for). ``mark`` is the ticker's
    high-water mark, the highest fence ever published for it, on any day's
    report; ``lock`` the ticker's lock, held around the comparison, the
    switch and the mark's raise. Lock order: a report's `publish_lock`
    first, then ``lock`` (the request counter takes ``lock`` alone, and
    nothing that holds ``lock`` waits for a publish lock), so no two holders
    wait on each other."""

    number: int
    mark: Path
    lock: Path


# Inside a superseded generation's directory: it was never live. Hidden, and
# not one of a run's files, so readers of a generation never meet it.
SUPERSEDED_MARK = ".superseded"


class ForeignPointer(OSError):
    """``.generations/<base>/current`` is a link this engine did not write:
    it names no generation directory beside it (``_generation_dir``), and is
    never followed. EINVAL, as before, but its own type: a copy with its
    links dereferenced (the pointer a directory) is a plain OSError, and a
    caller that must tell "nothing of ours is live" from "this directory is
    not ours to read" (``restore``) can (cross-branch review of the finding
    5 fix, finding 1)."""


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


def own_dir(d: Path) -> Path:
    """``d``, a directory the engine creates beneath one of the operator's
    roots, refused when it is a symlink: everything read or written under
    it would be wherever the link points (Hermes audit of 424b0b4, finding
    5). Here ``.staging``, ``.generations`` and ``.generations/<base>`` in a
    reports directory; elsewhere a vintage store's ``CIK##########``, a
    brief's ``<T>`` and ``<T>/<day>`` work directories and the brief queue.
    Only those: the roots themselves (``reports/``, ``journal/``, the
    vintage store, the SEC cache, the drop folder) may be links, and are
    never passed here; they are the operator's to place, on another disk if
    they like.

    A check, then a use: a link swapped in between the two is still written
    through. ``O_NOFOLLOW`` where a file is then opened covers only its last
    component, never these directories above it; closing the gap needs every
    open made relative to a descriptor held on the directory (``openat``
    with ``O_DIRECTORY | O_NOFOLLOW``), which this does not do. What it stops
    is a link already in place, which is what was reproduced."""
    if d.is_symlink():
        raise OSError(errno.ELOOP, f"{d} is a symlink, not the directory this engine "
                      "created there; it is never followed")
    return d


def _home(report: Path) -> Path:
    return own_dir(own_dir(report.parent / GENERATIONS_DIR) / _base(report))


def _pointer(report: Path) -> Path:
    return _home(report) / CURRENT


def _generation_dir(report: Path, name: str) -> Path | None:
    """``name`` as one of ``report``'s generation directories, or None when
    it cannot be one: a generation is a plain name in ``.generations/<base>/``
    (all the pointer is ever written with) and a real directory, never a
    symlink. Anything else was not written by this engine, and following it
    read, and wrote an audit, outside the reports directory (Hermes audit of
    424b0b4, finding 5: a pointer aimed at ``/elsewhere`` pinned it as the
    live run and ``publish_audit`` wrote ``<base>_audit.md`` into it)."""
    gen = _home(report) / name
    if name in ("", ".", "..") or "/" in name or gen.is_symlink():
        return None
    return gen


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
    published" (Hermes re-audit F2), and so does a pointer that does not
    name one of its generations (``_generation_dir``, `ForeignPointer`): it
    is never followed out of the reports directory, and never read as
    "nothing published" either. ``restore`` writes the pointer anew."""
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
    gen = _generation_dir(report, target)
    if gen is None or not gen.is_dir():
        raise ForeignPointer(errno.EINVAL, f"{pointer} -> {target!r} names no generation "
                             "directory beside it: not a pointer this engine wrote, or its "
                             "generation was removed. It is not followed; `restore` a kept "
                             "generation, or remove the pointer by hand to publish afresh")
    return gen


def _seq(gen: Path) -> int:
    """A generation's publish order, from its name (``<stamp>_<seq>_<id>``)."""
    parts = gen.name.split("_")
    return int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else -1


def generations(report: Path) -> list[Path]:
    """Every whole generation of ``report`` kept on disk, in publish order.
    Not the pointer, not a hidden directory (a build or copy interrupted
    part way, or a publish that failed: ``_set_apart``), and not a directory
    holding no report."""
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
    # Not a hidden directory's: a failed publish (``_set_apart``) holds no number.
    taken = ([_seq(d) for d in home.iterdir() if d.is_dir() and not d.name.startswith(".")]
             if home.is_dir() else [])
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


class PublishBusy(TimeoutError):
    """`publish_lock` with a timeout found the lock held past it: its own
    type, so a caller can tell it from any other timeout (an OSError
    ETIMEDOUT is a TimeoutError too; review of eeb1e51, N-2)."""


@contextmanager
def sidecar_lock(lock: Path, *, timeout: float | None = None,
                 busy: str = "the lock") -> Iterator[None]:
    """An exclusive ``flock`` on the sidecar file ``lock`` (created, never
    followed: ``O_NOFOLLOW``), across processes. ``timeout`` (seconds): give
    up with `PublishBusy` ("<busy> has been held for over <timeout>s") if it
    is not free by then; None waits."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW: a link planted at the lock's name fails (ELOOP) rather than
    # create its target outside the directory (Hermes audit of 424b0b4,
    # finding 5).
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PublishBusy(
                            f"{busy} has been held for over {timeout:g}s") from None
                    time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing releases the lock


@contextmanager
def publish_lock(report: Path, *, timeout: float | None = None) -> Iterator[None]:
    """The lock every change to ``report``'s live names holds, across
    processes: publishes, ``set_aside`` and ``restore`` happen one after the
    other. Readers take no lock: a generation, once published, never
    changes. It is a sidecar in the staging directory; ``flock`` is
    advisory and unreliable over NFS, so the reports directory must be
    local, as the watchlist's lock already assumes.

    ``timeout`` (seconds): give up with `PublishBusy` if it is not free by
    then, for a caller that must not wait on a stalled holder (the review
    console's tick, holding a web worker; review of 68dbc24, L-2). None,
    the default and what every publisher uses: wait for it."""
    lock = own_dir(report.parent / STAGING_DIR) / f"{_base(report)}.lock"
    with sidecar_lock(lock, timeout=timeout, busy=f"{report.name}: a publish's lock"):
        yield


# One whole number in ASCII digits, as `write_count` writes it: "07", "٣", a
# sign or a second line is not a count this engine wrote.
_COUNT_RE = re.compile(r"(?:0|[1-9][0-9]*)\n?", re.ASCII)


def read_count(path: Path) -> int | None:
    """The whole number in the small file at ``path`` (a request counter, a
    high-water mark), or None when there is no file. Never followed: a
    symlink is ELOOP. Any other failure to read raises OSError, and content
    that is not one whole number ValueError: neither is "no file"."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(64)
    # latin-1 reads any byte; only ASCII digits match the pattern.
    text = raw.decode("latin-1")
    if not _COUNT_RE.fullmatch(text):
        raise ValueError(f"{path} holds {raw[:20]!r}, not one whole number")
    return int(text)


def write_count(path: Path, n: int) -> None:
    """``n`` at ``path``, written whole and durable, its folder fsynced
    (`write_atomic(durable=True)`): a counter or a mark that a power loss
    could take back is no order at all."""
    write_atomic(path, f"{n}\n", durable=True)


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


@dataclass(frozen=True)
class Published:
    """One publish, as the process that made it saw it: ``live``, the live
    name; ``report``, the report's path in its own generation, which no later
    publish changes; and its ``generation_id``."""

    live: Path
    report: Path
    generation_id: str


_RECORDING: ContextVar[list[Published] | None] = ContextVar("report_files_recording",
                                                           default=None)


@contextmanager
def recording() -> Iterator[list[Published]]:
    """The publishes made inside the block, in order, as `Published`.

    Hermes re-audit of 84e65b0, finding 3: a command that published a report
    said only its live name, and the sweep chose the report to audit as the
    ticker's newest (another run's, published meanwhile, was audited and the
    entry stamped for it). Recorded here, where the publish happens, the
    generation is the one this command's own publish made, never read back
    from a live name another run can take in between. A context variable,
    so a builder's signature does not change and threads (the web UI) do
    not see each other's."""
    made: list[Published] = []
    token = _RECORDING.set(made)
    try:
        yield made
    finally:
        _RECORDING.reset(token)


def _git(*args: str) -> str | None:
    try:
        # errors="replace": with core.quotePath=false git prints a file name
        # in whatever bytes it has, and a stamp must never fail on one.
        proc = subprocess.run(["git", *args], cwd=_ENGINE_ROOT, capture_output=True,
                              text=True, errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


@functools.cache
def engine_commit() -> str:
    """The commit this engine runs from, as a report states it: the short
    sha and whether the engine code differs from it. Read once per process,
    when this module is imported (below): the code a running process has
    loaded is the code it started with."""
    stated = os.environ.get(ENGINE_ENV, "").strip()
    if stated:
        return f"{stated} (stated by {ENGINE_ENV}; not a git checkout)"
    # The engine's own checkout, not one it happens to sit inside (an
    # installed copy under a venv in some other repository).
    top = _git("rev-parse", "--show-toplevel")
    sha = _git("rev-parse", "--short=12", "HEAD")
    if not (top and sha and Path(top).resolve() == _ENGINE_ROOT.resolve()):
        return "unknown (not run from a git checkout; set FQE_ENGINE_COMMIT)"
    # Untracked files count: a new module the engine imports changes what it
    # does as much as an edit (caches are gitignored, so they never count).
    changed = _git("status", "--porcelain", "--untracked-files=normal", "--", *_ENGINE_PATHS)
    if changed is None:
        return f"{sha} (could not check the checkout for uncommitted changes)"
    if changed:
        return f"{sha} + uncommitted changes to the engine code (not reproducible from {sha})"
    return f"{sha} (clean checkout)"


# Read now, when the engine's code is loaded: a long-lived process (the web
# UI) that publishes after a `git pull` must name the code it is running, not
# the checkout's new HEAD. Never fatal at import: a failure is read again at
# the first publish, and stamped "unknown" there if it fails again.
with contextlib.suppress(Exception):
    engine_commit()


def _seal(staged: Staged, name: str, fence: Fence | None = None) -> None:
    """A publish is one whole generation or nothing: both files must exist,
    and each is stamped with the rebuild's id (the report on its last line,
    the ledger in `generation_id`). A file already naming another generation
    was not written by this rebuild. A fenced rebuild's ledger is stamped
    with its ``fence`` too (only then: an unfenced ledger is as it was)."""
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
    if fence is not None:
        doc["fence"] = fence.number
    try:
        engine = engine_commit()
    except Exception as e:  # noqa: BLE001 - the stamp is metadata; never a reason not to publish
        engine = f"unknown (the engine commit could not be read: {type(e).__name__}: {e})"
    doc["engine_commit"] = engine
    write_atomic(staged.ledger, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    if m is None:
        # Appended in place (the staged file is this rebuild's own), never
        # rewritten: what the builder wrote is the report, byte for byte, up
        # to the stamp.
        with staged.report.open("a", newline="") as fh:
            fh.write(f"\n\n{ENGINE_LINE}{engine}\n{GENERATION_LINE}{gid} "
                     "(this report, its evidence ledger and its audit carry the same id)\n")
    for path in (staged.report, staged.ledger):
        os.chmod(path, READ_ONLY)
        _fsync(path)
    _fsync(staged.report.parent)


def existing_mode(path: Path) -> int | None:
    """The permission bits of the regular file at ``path``, which a file
    written whole over it keeps; None when there is none there (nothing, or
    a symlink, which is not followed, or anything else) or when another user
    owns it. Only ``0o777``: setuid, setgid and sticky are never lent to the
    engine's text, and a file someone else planted at the name lends nothing
    at all — the new one gets the umask's, as a new file does (review of the
    finding 5 fix)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return None
    return st.st_mode & 0o777


def write_atomic(path: Path, text: str, *, mode: int | None = None,
                 durable: bool = False) -> None:
    """Write ``text`` to ``path`` through a temporary file beside it,
    fsynced, and ``os.replace``: a reader sees the old file or the new one,
    and a failed write leaves no temporary behind. ``mode``, when given, is
    set before the file takes its name; otherwise a regular file already
    there keeps its own (a state file made 0o600 stays so; the temporary
    has the umask's, which a new file keeps), as the journal's entries do.
    A symlink at the name lends nothing: it is replaced, never followed.
    ``durable``: the folder is fsynced after the rename too, without which
    the rename itself can be lost on power loss (Hermes re-audit of #118 @
    34836cf); for state whose loss matters (the workbench's request
    counter and high-water mark)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is None:
            mode = existing_mode(path)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if durable:
            _fsync(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


@contextmanager
def replacing(report: Path, *, now: datetime | None = None,
              timeout: float | None = None, fence: Fence | None = None) -> Iterator[Staged]:
    """Build a report's replacement off to the side, then publish it.

    Inside the block the caller builds into ``staged.report`` and
    ``staged.ledger`` (in ``<dir>/.staging/<id>/``, which no live-report glob
    reads). If the block raises, or returns without both (or with one naming
    another generation, ``NotPublished``), the staged directory is removed
    and nothing live changes. Otherwise, holding ``publish_lock``, the
    stamped generation is renamed whole into ``.generations/<base>/`` and the
    one pointer swapped to it (the module docstring). Every step before the
    swap leaves the earlier generation live; the swap is one rename; a
    failure after it swaps the earlier generation back before raising
    (``_switch``).

    The staging directory is shared by every rebuild writing to ``<dir>`` and
    is never removed: a rebuild that removed it once it looked empty pulled
    it from under another still building there (round-9 audit F1).

    ``timeout``: `publish_lock`'s, for a caller that must not wait on a
    stalled holder (the workbench, whose run would otherwise show
    "running" for as long as the lock is held; review of 9d00328). Past it,
    `PublishBusy` and nothing published. None, the default: wait.

    ``fence``: the run's place in its ticker's requests (`Fence`; the
    workbench's, `workbench.fencing.fence`), its number sealed into the
    ledger. Under the publish lock and then the ticker's lock (`Fence`),
    immediately before the switch, the number is compared with the
    ticker's high-water mark and with the live generation's fence
    (`fence_of`): below either, the generation is kept in the archive,
    marked superseded, never made live, and `Superseded` is raised; equal or
    higher, or neither stated, it publishes and raises the mark to its
    number. A mark or live fence that cannot be read refuses the publish
    (`NotPublished`, the generation set apart). The ticker's lock is waited
    for at most ``timeout`` too. None (the journal, the auto track, the
    CLI): no comparison and no mark, the publish as it always was.
    """
    if fence is not None and fence.number < 0:
        raise ValueError(f"{report.name}: fence {fence.number} is not a request number "
                         "(0 or more)")
    gid = uuid.uuid4().hex
    work = own_dir(report.parent / STAGING_DIR) / gid
    work.mkdir(parents=True)
    names = _names(_base(report))
    staged = Staged(report=work / names["report"], ledger=work / names["ledger"],
                    generation_id=gid)
    try:
        yield staged
        _seal(staged, report.name, fence)
        with publish_lock(report, timeout=timeout), (
                contextlib.nullcontext() if fence is None else
                sidecar_lock(fence.lock, timeout=timeout,
                             busy=f"{report.name}: the ticker's lock")):
            _publish(report, staged, work, now, fence)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # gone already once published


def _publish(report: Path, staged: Staged, work: Path, now: datetime | None,
             fence: Fence | None = None) -> None:
    """The publish; the caller holds ``publish_lock`` (and, fenced, the
    ticker's lock). What it reports as archived is read before the switch.
    The fences are read here, under the locks and immediately before the
    switch, so no publish can come between the comparison and the switch it
    decides.

    Fenced, the high-water mark is raised BEFORE the switch, written and
    its folder fsynced (Hermes re-audit of #118 @ 34836cf: raised after it,
    a process killed between the two left the run live above the mark, and
    an older run for another report day saw only the mark). A kill in
    between leaves the mark ahead of the live run: the safe direction (an
    older run is refused; the next request is numbered above it). Any
    failure or interrupt once the pointer may have moved switches it back
    first, then sets the generation apart (set apart while the pointer
    still named it, it dangled); a switch back that fails is
    `PublishInDoubt`, and the generation stays where the pointer may name
    it. A publish that ends without its run live puts the mark back."""
    home = _home(report)
    home.mkdir(parents=True, exist_ok=True)
    _adopt(report, now)
    previous = current_generation(report)
    archived = [] if previous is None else [
        p for name in _names(_base(report)).values() if (p := previous / name).exists()]
    gen = home / _name_next(report, _stamp(now), staged.generation_id)
    try:
        held, mark = (None, None) if fence is None else _held_fence(report, fence)
    except NotPublished:
        os.rename(work, gen)
        _set_apart(gen)  # kept for diagnosis; never a run
        raise
    superseded = held is not None and fence is not None and fence.number < held
    if superseded:
        (work / SUPERSEDED_MARK).write_text(f"superseded: request {held} had published\n")
    os.rename(work, gen)
    what = f"publishing {gen.name}"
    marked = switched = False
    try:
        _fsync(home)
        if not superseded:
            if fence is not None:
                marked = True
                write_count(fence.mark, fence.number)  # write-ahead
            _link_live_names(report)
            # Set before the call: `_switch` may move the pointer and then be
            # interrupted, and switching back to `previous` is harmless when
            # it never moved.
            switched = True
            _switch(report, gen, previous, what)
    except PublishInDoubt:
        raise  # the new generation may be live: it stays where the pointer may name it
    except BaseException as e:
        if switched:
            _switch_back(report, gen, previous, what, e)  # PublishInDoubt: gen stays put
        try:
            if marked:
                assert fence is not None
                _restore_mark(fence, mark)
        finally:
            _set_apart(gen)  # never live, or switched back: not a run
        if marked and not switched and isinstance(e, OSError):
            assert fence is not None
            raise NotPublished(
                f"{report.name}: raising the high-water mark to {fence.number} failed "
                f"({type(e).__name__}: {e}); nothing published, the live run is unchanged") from e
        raise
    if superseded:
        # Kept whole in the archive, where `generations` lists it and
        # `restore` can name it; the live run is untouched.
        assert fence is not None and held is not None
        raise Superseded(report, gen, staged.generation_id, fence.number,
                         None if previous is None else generation_of(previous / report.name),
                         held)
    staged.archived.extend(archived)
    made = _RECORDING.get()
    if made is not None:
        made.append(Published(report, gen / report.name, staged.generation_id))


# A report's base name, `<T>_<YYYY-MM-DD>`: its ticker and its day. A replay
# (`.replay`) is not one of the ticker's live days.
_DAY_RE = re.compile(r"(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})", re.ASCII)


def _day_reports(report: Path) -> list[Path]:
    """``report`` and every other day's report of its ticker in its folder
    (those with a generations folder: never published, nothing is live)."""
    found = {report}
    m = _DAY_RE.fullmatch(_base(report))
    gens = own_dir(report.parent / GENERATIONS_DIR)
    if m is not None and gens.is_dir():
        for d in gens.iterdir():
            day = _DAY_RE.fullmatch(d.name)
            if day is not None and day.group(1) == m.group(1):
                found.add(report.with_name(f"{d.name}.md"))
    return sorted(found)


def _held_fence(report: Path, fence: Fence) -> tuple[int | None, int | None]:
    """(the fence a run must reach to publish, the mark as read). The
    fence is the highest of the ticker's high-water mark and the live fence
    of EVERY report day of the ticker in this folder (None when none states
    one). The mark holds across days and restores (review of 2cbba1c); the
    days' live runs cover a mark that is lost or rewound (a power loss, an
    old backup, runs fenced before it existed; Hermes re-audit of #118 @
    34836cf). A generation set apart or superseded is never a day's live
    one. Any of them that cannot be read refuses the publish: a failed read
    is not "no fence"."""
    try:
        mark = read_count(fence.mark)
    except (OSError, ValueError) as e:
        why = errno.errorcode.get(e.errno, str(e.errno)) if isinstance(e, OSError) and e.errno \
            else str(e)
        raise NotPublished(f"{report.name}: the ticker's high-water mark unreadable ({why}: "
                           f"{fence.mark}); nothing published, the live run is unchanged") from e
    stated = [] if mark is None else [mark]
    try:
        for day in _day_reports(report):
            gen = current_generation(day)
            live = None if gen is None else fence_of(day, gen)
            if live is not None:
                stated.append(live)
    except OSError as e:
        why = errno.errorcode.get(e.errno, str(e.errno)) if e.errno else str(e)
        raise NotPublished(f"{report.name}: live fence unreadable ({why}: {e.strerror or e}); "
                           "nothing published, the live run is unchanged") from e
    return (max(stated) if stated else None), mark


def _restore_mark(fence: Fence, mark: int | None) -> None:
    """The mark as it was before a publish that did not make its run live:
    safe under the ticker's lock, which no other publish or request holds
    meanwhile. One that cannot be put back stays ahead, the safe
    direction."""
    with contextlib.suppress(OSError):
        if mark is None:
            fence.mark.unlink(missing_ok=True)
            _fsync(fence.mark.parent)
        else:
            write_count(fence.mark, mark)


def fence_of(report: Path, gen: Path) -> int | None:
    """The fence ``gen`` (one of ``report``'s generations) was sealed with:
    its ledger's ``fence``, when that is a whole number of zero or more.
    None for a generation without one — published unfenced (the journal,
    the auto track, the CLI), from before fences, adopted plain files — and
    for a ledger that is missing, not JSON, a symlink (planted:
    never followed, as `read_live` refuses it), or that names anything
    else as its fence. None holds nothing back: a run is superseded only by
    a fence the live run states, never by one guessed at, which would leave
    the ticker stuck behind an unreadable ledger.

    A ledger that is there and cannot be READ (EMFILE, EIO, EACCES, ...)
    raises OSError: that is not "no fence", and a fenced publish is refused
    on it (review of 2cbba1c, M1)."""
    ledger = gen / _names(_base(report))["ledger"]
    if ledger.is_symlink():
        return None
    try:
        doc = json.loads(ledger.read_text())
    except (FileNotFoundError, ValueError):
        return None
    fence = doc.get("fence") if isinstance(doc, dict) else None
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 0:
        return None
    return fence


def _set_apart(gen: Path) -> None:
    """A generation whose publish failed, and that is not live (never
    switched to, or switched back): renamed ``.failed-<name>``, hidden, so
    it is kept for diagnosis but is not a run — ``generations`` does not
    list it, ``restore`` cannot name it, and it holds no sequence number
    (review of the finding-3a fix: it stayed in ``.generations/<base>/``
    looking like an ordinary archived run). A rename that fails leaves it
    where it is; the publish's own error is the one raised."""
    with contextlib.suppress(OSError):
        os.rename(gen, gen.with_name(f".failed-{gen.name}"))
        _fsync(gen.parent)


def _switch(report: Path, gen: Path | None, previous: Path | None, what: str) -> None:
    """Point ``report``'s live names at ``gen`` (None: at nothing, a set-aside)
    from ``previous``, so that a raised error means ``previous`` is live. The
    caller holds ``publish_lock``.

    Hermes audit of 424b0b4, finding 3a: the publish swapped the pointer, then
    fsynced the directory and re-linked the audit; either failing raised
    (the command said "failed", the operator was told the earlier run stayed
    live) with the new generation live. Now every step that can fail runs
    before the pointer's rename. After it only the directory's fsync (what
    makes the rename durable) and, for a restored generation with an audit,
    that audit's link; if either fails the earlier pointer is put back
    (``_switch_back``) before anything is raised."""
    audit = _companions(report)["audit"]
    link_its_audit = gen is not None and (gen / audit.name).is_file()
    try:
        # The audit's live name resolves through the pointer, so left in place
        # it would name ``gen``'s audit from the switch on (a freshly published
        # generation never has one). It goes first: until the switch the
        # earlier run is live without its audit at the live name, the safe
        # direction (an audit missing, never another run's beside a report).
        if audit.is_symlink():
            audit.unlink()
            _fsync(report.parent)
        if gen is None:
            _pointer(report).unlink()  # the switch, to nothing
        else:
            _symlink(_pointer(report), gen.name)  # the switch: one rename
        _fsync(_home(report))
        if link_its_audit:
            link_audit(report)
    except BaseException as e:
        _switch_back(report, gen, previous, what, e)
        if not isinstance(e, Exception):
            raise  # an interrupt stays one, raised with the earlier run live
        back = "no run is live, as before" if previous is None else f"{previous.name} is live again"
        raise NotPublished(
            f"{report.name}: {what} failed ({type(e).__name__}: {e}); the switch was undone "
            f"and read back from the pointer: {back}") from e


def _switch_back(report: Path, gen: Path | None, previous: Path | None, what: str,
                 failure: BaseException) -> None:
    """After a failed ``_switch``: point the live names at ``previous`` again
    (nothing, if there was none), with its audit, and read the pointer back.
    Anything short of that raises `PublishInDoubt`: the new generation may
    be live, and saying the earlier one is would be a guess."""
    pointer = _pointer(report)
    try:
        if previous is None:
            pointer.unlink(missing_ok=True)
        else:
            _symlink(pointer, previous.name)
        _fsync(pointer.parent)
        link_audit(report)
        back = current_generation(report)
    except BaseException as e:
        why, cause = f"switching back failed ({type(e).__name__}: {e})", e
    else:
        if back == previous:
            return
        why, cause = f"switching back did not take (the pointer names {back})", failure
    maybe = (f"the NEW generation {gen.name} may be live" if gen is not None
             else "the run may be set aside, with no run live")
    # How to put it right, named in full: this is read by an operator at the
    # end of a failed command, not by code.
    fix = (f"report_files.restore({str(report)!r}, {previous.name!r}) makes {previous.name} "
           "live again" if previous is not None else
           f"report_files.set_aside({str(report)!r}) takes the new run off the live names")
    raise PublishInDoubt(
        f"{report.name}: {what} failed ({type(failure).__name__}: {failure}), and {why}: "
        f"{maybe}. Check which run is live with `readlink {pointer}`; {fix}.") from cause


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
        _switch(report, None, gen, "setting it aside")
        return [p for name in _names(_base(report)).values() if (p := gen / name).exists()]


def restore(report: Path, generation: str) -> Path:
    """Make a kept generation live again, in one step: ``generation`` is its
    directory name or a unique part of it (its id, its stamp). Returns the
    restored report's path in its generation. A plain file at a live name
    that is not the live run's stops it, as it stops a rebuild. A pointer
    this engine did not write (`ForeignPointer`) names no live run to keep,
    and is written anew."""
    with publish_lock(report):
        _home(report).mkdir(parents=True, exist_ok=True)
        try:
            _adopt(report, None)
        except ForeignPointer:
            # Read (before any change) as the run plain files at the live
            # names must match. Dropped, those files are kept as a generation
            # of their own, as with no pointer at all (cross-branch review of
            # the finding 5 fix, finding 1).
            _pointer(report).unlink()
            _adopt(report, None)
        kept = generations(report)
        matches = ([d for d in kept if d.name == generation]
                   or [d for d in kept if generation in d.name])
        if len(matches) != 1:
            raise ValueError(f"{report.name}: {len(matches)} generations match {generation!r}")
        (gen,) = matches
        try:
            previous = current_generation(report)
        except ForeignPointer:
            # No live run to keep or to put back on a rollback: the pointer is
            # replaced, never followed (a dereferenced copy is still refused).
            previous = None
        _link_live_names(report)
        _switch(report, gen, previous, f"restoring {gen.name}")
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


def _refuse_links(gen: Path, paths: dict[str, Path]) -> None:
    """A generation's files are written by `_seal` and `publish_audit`,
    never as links: one that is a link was put there by hand, and reading
    through it showed a file from anywhere as the run's report, or paired
    one naming the run's generation as its ledger or audit (independent
    review of 9d00328). Refused (ELOOP, naming it), never followed. An
    lstat, then a read: the generation is read-only, and what this stops is
    a link already in place (`own_dir`'s reasoning)."""
    for path in paths.values():
        if path.is_symlink():
            raise OSError(errno.ELOOP, f"{path.name} in generation {gen.name} is a symlink, "
                          "not a file this engine wrote; it is never followed. Remove it, "
                          "or restore another generation")


def read_live(report: Path) -> LiveRun | None:
    """The live run of ``report``, pinned to one generation: the pointer is
    resolved once and every file is read from that generation. None when no
    run is live. Given a generation's own path, that generation is read
    (live or not). Files from before generations are read at their live
    names, and a report and ledger that both name no generation still pair.
    A path through the pointer (``.generations/<base>/current/<base>.md``,
    what ``readlink`` of a live name gives) is the live run's, resolved
    once like the live name's."""
    gen: Path | None
    if live_name(report) != report and report.parent.name == CURRENT:
        gen = current_generation(live_name(report))  # None: set aside, nothing to read
    elif live_name(report) != report:
        gen = _generation_dir(live_name(report), report.parent.name)
        if gen is None:
            raise OSError(errno.EINVAL, f"{report.parent} is not a generation this engine "
                          "wrote (a symlink, or not a plain name)")
    else:
        gen = current_generation(report)
    if gen is None and report.is_symlink():
        return None  # the live names exist but name no run (set aside)
    paths = (_companions(report) if gen is None
             else {role: gen / name for role, name in _names(_base(report)).items()})
    if gen is not None:
        _refuse_links(gen, paths)
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
