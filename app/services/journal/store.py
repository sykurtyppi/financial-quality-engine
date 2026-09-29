"""Journal entry store — the single source of truth for the decision-impact
journal's markdown entries. Both the CLI and the web UI call this module, so an
entry opened in one is identical to one opened in the other.

The one methodological invariant lives here: a report cannot be generated until a
real BEFORE thesis exists (``has_thesis``), and once generated the entry is
stamped ``reported`` so it cannot be regenerated (no peek-then-edit).

Every write is whole or not at all (Hermes audit of the stack): an entry is
written to a temporary file beside it, fsynced and renamed into place, so a
process killed mid-write leaves the old entry or the new one — never the
truncated "reported: 20" a plain ``write_text`` left. Read-modify-write
updates hold a per-entry lock across the read, the change and the write (v1:
``mark_reported`` / ``set_field``; v2: ``update_v2``), so the sweep, the web
UI and ``journal.py`` updating one entry at once never lose one another's
change. Generating an entry's report holds a second, longer lock
(``report_lock``) from the "not reported yet" check to the stamp, so one
entry's report is built and published once; a report deferred for the
sweep's audit (``--defer-mark``) stays PENDING (``set_report_pending``)
until it is stamped, and no plain report is built over it meanwhile.
Entries are UTF-8 on disk and are read as UTF-8, whatever the locale.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENTRIES = ROOT / "journal" / "entries"

IMPACT_CODES = ("changed_thesis", "changed_confidence", "new_investigation", "no_value")
VERDICTS = ("helped", "neutral", "hurt", "too_early")
CONVICTION_CHOICES = tuple(str(n) for n in range(1, 6))

# The protocol's biggest failure mode is forgetting to close the OUTCOME block
# weeks later. An entry reported this long ago without a verdict is "stale".
STALE_OUTCOME_DAYS = 14

# Fields whose template line carries a trailing "# guidance" comment. Only these
# get the comment stripped on read — never user free-text (which may contain '#').
_COMMENT_FIELDS = frozenset({"conviction", "impact", "conviction_after", "verdict"})

# Ticker must not escape the entries dir or break the filename scheme: leading
# alphanumeric (rejects "..", ".", "-"), then A-Z/0-9/./- only, max 12 chars.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
# Round-14 finding 3: the `day` half of TICKER_DATE.md was unvalidated. Direct
# traversal was blocked in practice (no matching file), but any user-supplied
# `date=` value reached path construction. Strict ISO-day format only.
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Where a hard link cannot be made (FAT/exFAT, some SMB and FUSE mounts), a
# create falls back to a rename after an existence check — still whole, and
# still no-clobber because every creator holds the entry lock.
_NO_HARD_LINKS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS, errno.EXDEV})


@contextmanager
def _flock(lock: Path) -> Iterator[None]:
    """Hold an exclusive `flock` on the sidecar file ``lock``. Each call opens
    its own descriptor, so two threads of one process exclude each other as
    two processes do. `flock` is advisory and not reliable over NFS — the
    same assumption as the watchlist's `_write_lock` and the reports'
    `publish_lock`."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


@contextmanager
def _entry_lock(path: Path) -> Iterator[None]:
    """Exclusive, cross-process, for one entry's read-modify-write: a sidecar
    ``.<name>.lock`` beside it, because the entry itself is replaced (a lock
    on the old inode would not exclude a writer of the new one)."""
    with _flock(path.with_name(f".{path.name}.lock")):
        yield


@contextmanager
def report_lock(path: Path) -> Iterator[None]:
    """Exclusive, cross-process, for generating one entry's report. A report
    command holds it from its "not reported yet" check (made after taking
    it) through the build and publish to the ``reported`` stamp; with
    ``--defer-mark``, through the publish, and ``mark-reported`` takes it for
    the stamp. So of two report commands on one entry (the CLI, the web UI,
    the sweep) the second waits, then finds the entry stamped (or, after a
    ``--defer-mark``, its report pending: ``set_report_pending``) and
    refuses before it builds or publishes anything.

    Hermes audit of 424b0b4, finding 3b: two commands at once each checked,
    built and published; the second's stamp was then refused ("already
    reported", exit 1) with its report the live one.

    A sidecar of its own (``.<name>.report.lock``), not the entry lock: a
    report takes minutes, and the entry lock held that long would stop every
    other update of the entry; the stamp takes the entry lock itself, inside
    this one (always in that order, never the other)."""
    with _flock(path.with_name(f".{path.name}.report.lock")):
        yield


def _pending_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.report.pending")


def set_report_pending(path: Path, by: str) -> None:
    """Record that ``path``'s report is PENDING its audit: built (or being
    built) by ``journal.py report --defer-mark`` and not yet stamped. The
    caller holds ``report_lock``.

    Review of the finding-3b fix: ``--defer-mark`` releases the report lock
    once it has published, and the sweep then audits that report with the
    entry unstamped (minutes). A plain ``report`` (or the web page) in that
    window found "not reported", built, published over the run being
    audited and stamped; the sweep's audit then failed, or its
    ``mark-reported`` found "already reported". While this marker is there a
    plain report refuses (``journal.py report --retry`` overrides it), and
    ``mark-reported`` clears it once it has stamped.

    A hidden sidecar (``.<name>.report.pending``), written whole: when, who
    (the command, its pid and host), so a refusal can say what it waits
    for."""
    marker = _pending_path(path)
    text = f"marked {now_iso()} by {by}, pid {os.getpid()} on {os.uname().nodename}\n"
    _durable_write(marker, text, create=not marker.exists())


def report_pending(path: Path) -> str | None:
    """What ``set_report_pending`` recorded for ``path``, or None when its
    report is not pending. A marker that is there but cannot be read still
    pends (fail closed): reading it as "nothing pending" would let a plain
    report publish over the run the sweep is auditing."""
    try:
        return _pending_path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"(the marker {_pending_path(path).name} cannot be read: {e})"


def clear_report_pending(path: Path) -> None:
    """The report is no longer pending (``mark-reported`` stamped it, or a
    ``--retry`` rebuilt and stamped it). The caller holds ``report_lock``."""
    try:
        _pending_path(path).unlink()
    except FileNotFoundError:
        return
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)  # the removal survives a crash, as the stamp does
    finally:
        os.close(dir_fd)


def _durable_write(path: Path, text: str, *, create: bool = False) -> None:
    """Write ``path`` whole or not at all: a temporary file beside it,
    fsynced, then renamed over it (``create``: hard-linked into place, which
    fails with FileExistsError rather than replace an entry that appeared in
    the meantime — the no-overwrite rule holds under a race too). The
    directory is fsynced after, so the new name survives a crash.

    A new entry gets the permissions the umask gives any new file (0o644, or
    0o600 under ``umask 077``), as ``write_text`` did; an update keeps the
    entry's own."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 0o666 on a create: the kernel applies the umask, which is how every
        # other new file gets its mode (os.umask would be process-wide).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if create else 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if create:
            try:
                os.link(tmp, path)
            except OSError as e:
                if e.errno not in _NO_HARD_LINKS:
                    raise
                if path.exists():
                    raise FileExistsError(path) from None
                os.replace(tmp, path)
        else:
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
            os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def safe_ticker(ticker: str) -> str:
    """Uppercased ticker restricted to a safe charset. Raises ValueError on
    anything with path separators, `..`, or that could break `TICKER_DATE.md`."""
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.match(t):
        raise ValueError(f"invalid ticker: {ticker!r}")
    return t


def safe_day(day: str) -> str:
    """Enforce YYYY-MM-DD on the day component of TICKER_DAY.md paths.
    Rejects `../`, whitespace, arbitrary strings — mirrors `safe_ticker` for
    the other filename component. Round-14 finding 3."""
    if not day or not _DAY_RE.match(day):
        raise ValueError(f"invalid day (expected YYYY-MM-DD): {day!r}")
    return day


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def entry_path(ticker: str, day: str | None = None) -> Path:
    d = safe_day(day) if day else today()
    return ENTRIES / f"{safe_ticker(ticker)}_{d}.md"


def find_entry(ticker: str, day: str | None = None) -> Path | None:
    if day:
        p = entry_path(ticker, day)  # safe_day validates before path construction
        return p if p.exists() else None
    matches = sorted(ENTRIES.glob(f"{safe_ticker(ticker)}_*.md"))
    return matches[-1] if matches else None


def list_entries() -> list[Path]:
    return sorted(ENTRIES.glob("*.md")) if ENTRIES.exists() else []


def field(text: str, key: str) -> str | None:
    """Value of ``key:`` on its OWN line. Uses ``[ \\t]*`` (not ``\\s*``) so a blank
    field does not bleed into the next line's text. The template's ``# guidance``
    comment is stripped ONLY for the enum/numeric fields that carry one — never
    from user free-text, which may legitimately contain ``#``."""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1)
    if key in _COMMENT_FIELDS:
        val = val.split("#")[0]
    val = val.strip()
    return val or None


def has_thesis(text: str) -> bool:
    t = field(text, "thesis")
    return bool(t) and not t.startswith("<")


def is_reported(text: str) -> bool:
    # A value on the SAME line only — `\s` would span the newline into the next
    # heading and false-trip on a fresh entry.
    return bool(re.search(r"^reported:[ \t]*\S", text, re.MULTILINE))


def days_since(iso_ts: str | None, now: datetime | None = None) -> int | None:
    """Whole days elapsed since ``iso_ts`` (as written by ``now_iso``), or None if
    ``iso_ts`` is missing/unparseable. ``now`` is injectable for tests."""
    if not iso_ts:
        return None
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return ((now or datetime.now(UTC)) - dt).days


def open_entry(
    ticker: str,
    thesis: str | None = None,
    conviction: int | str | None = None,
    action: str | None = None,
) -> Path:
    """Create a new entry. Raises ValueError on a bad ticker, FileExistsError if
    one already exists for today."""
    t = safe_ticker(ticker)
    ENTRIES.mkdir(parents=True, exist_ok=True)
    path = entry_path(t)
    if path.exists():
        raise FileExistsError(path)  # the common case, said before any work
    # Whitespace-only thesis -> placeholder, so the report lock still refuses it
    # (unifies CLI and web, which both must treat a blank thesis as "no thesis").
    if thesis is not None and not thesis.strip():
        thesis = None
    thesis = thesis or "<one or two sentences: your view BEFORE reading the report>"
    conviction = conviction if conviction is not None else "<1-5>"
    action = action or "<hold / trim / add / avoid / no position>"
    # Under the lock: a create where no hard link can be made checks, then
    # renames (`_durable_write`).
    with _entry_lock(path):
        _durable_write(path, (
            f"# {t} — {today()}\n"
            f"opened: {now_iso()}\n"
            f"reported:\n\n"
            f"## BEFORE  (write before reading the report)\n"
            f"thesis: {thesis}\n"
            f"conviction: {conviction}        # 1 (low) - 5 (high)\n"
            f"intended_action: {action}\n\n"
            f"## AFTER  (fill after reading the report)\n"
            f"impact:                         # any of: {', '.join(IMPACT_CODES)}\n"
            f"conviction_after:               # 1-5\n"
            f"what_it_surfaced:\n"
            f"what_i_disagreed_with:\n\n"
            f"## OUTCOME  (fill weeks later)\n"
            f"outcome_date:\n"
            f"what_happened:\n"
            f"verdict:                        # helped / neutral / hurt / too_early\n"
        ), create=True)
    return path


def mark_reported(path: Path) -> None:
    with _entry_lock(path):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^reported:[ \t]*$", f"reported: {now_iso()}", text, count=1,
                      flags=re.MULTILINE)
        _durable_write(path, text)


def set_field(path: Path, key: str, value: str) -> None:
    """Write ``key: value`` on the field's single line. Newlines in ``value`` are
    collapsed to spaces so a multiline textarea can't break the one-field-per-line
    structure the parser assumes. A function replacement is used so backslashes or
    ``\\g`` sequences in user text are treated literally, not as regex refs."""
    value = re.sub(r"\s*\n\s*", " ", value).strip()
    with _entry_lock(path):
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(rf"^{re.escape(key)}:.*$", lambda _m: f"{key}: {value}", text,
                         count=1, flags=re.MULTILINE)
        if n:
            _durable_write(path, new)


def parse_entry(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    impact = field(text, "impact")
    return {
        "name": path.stem,
        "ticker": path.stem.split("_")[0],
        "day": path.stem.split("_", 1)[1] if "_" in path.stem else "",
        "opened": field(text, "opened"),
        "reported": field(text, "reported"),
        "thesis": field(text, "thesis"),
        "conviction": field(text, "conviction"),
        "intended_action": field(text, "intended_action"),
        "impact": impact,
        "conviction_after": field(text, "conviction_after"),
        "what_it_surfaced": field(text, "what_it_surfaced"),
        "what_i_disagreed_with": field(text, "what_i_disagreed_with"),
        "outcome_date": field(text, "outcome_date"),
        "what_happened": field(text, "what_happened"),
        "verdict": field(text, "verdict"),
        "has_thesis": has_thesis(text),
        "is_reported": is_reported(text),
        "needs_after": impact is None,
        "needs_outcome": field(text, "verdict") is None,
        "days_since_reported": days_since(field(text, "reported")),
    }


# ---------------------------------------------------------------------------
# v2 storage (P1-E). Same TICKER_DATE.md filename convention; entries are told
# apart by the ---json front-matter fence. v1 entries continue to parse via the
# functions above; a mixed journal is fine.
# ---------------------------------------------------------------------------


def is_v2(path: Path) -> bool:
    """True iff the file starts with the v2 front-matter fence."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return f.readline().rstrip("\n") == "---json"
    except OSError:
        return False


def save_v2(entry, path: Path | None = None, *, allow_update: bool = False) -> Path:
    """Serialize a v2 entry to disk. Defaults to `journal/entries/TICKER_DAY.md`.

    Round-12 finding 1 (the whole tamper-evidence guarantee): by DEFAULT this
    refuses to overwrite ANY existing entry — v1 or v2. Repeating `openv2` for
    the same TICKER_DATE used to silently replace the earlier preregistration
    with a new hash, which broke the "no peek-then-edit" protocol at the
    filesystem layer (the on-disk file passed `verify_lock` because it carried
    a fresh matching hash — the ORIGINAL commitment was simply gone).

    Legitimate updates (persisting a new resolution via `resolve --commit`;
    stamping `reported` from the report step) pass `allow_update=True`. Those
    call sites must not change the BEFORE block, and the guard verifies that:
    the incoming entry's `before_sha256` MUST equal the on-disk entry's, and
    it is held to what `update_v2` holds a change to (the sealed fields as
    on disk, the lock verified, a `reported` stamp never cleared or moved).
    Anything else is refused. An update of an entry loaded earlier goes
    through `update_v2`, which re-reads it under the lock.
    """
    from app.services.journal.schema_v2 import (
        render_entry,  # avoid import cycle at load
    )

    target = path or entry_path(entry.ticker, entry.day.isoformat())
    ENTRIES.mkdir(parents=True, exist_ok=True)
    # The checks below and the write are one step: another writer between
    # them could otherwise replace the entry this call just vetted.
    with _entry_lock(target):
        return _save_v2_locked(entry, target, allow_update, render_entry)


def _save_v2_locked(entry, target: Path, allow_update: bool, render_entry) -> Path:
    existed = target.exists()
    if existed:
        if not is_v2(target):
            raise FileExistsError(
                f"v1 entry exists at {target}; refusing to overwrite. Move or delete it first."
            )
        if not allow_update:
            raise FileExistsError(
                f"v2 entry already exists at {target}; refusing to overwrite. "
                "A locked preregistration cannot be replaced (would erase the "
                "tamper-evidence guarantee). Use a different --date, delete "
                "the file explicitly, or update in place via `resolve`/`report`."
            )
        # Update path: BEFORE must match the on-disk hash. This defends against
        # any caller that accidentally passes allow_update=True with a modified
        # BEFORE block. Compares hashes (not full BEFORE) to avoid loading a
        # possibly corrupt on-disk BEFORE into a pydantic model.
        try:
            existing = load_v2(target)
        except (ValueError, OSError) as e:  # noqa: BLE001 - guard, not silent-swallow
            raise ValueError(f"cannot update {target}: existing file unreadable ({e})") from e
        if existing.before_sha256 != entry.before_sha256 or entry.before_sha256 is None:
            existing_h = (existing.before_sha256 or "")[:16] or "<unset>"
            new_h = (entry.before_sha256 or "")[:16] or "<unset>"
            raise ValueError(
                f"refusing to update {target}: BEFORE hash changed "
                f"({existing_h} -> {new_h}). Updates must preserve the locked BEFORE block."
            )
        # Held to what `update_v2` holds its change to (review of the
        # finding-4 fix): comparing the two hashes alone let a save clear a
        # `reported` stamp, or write a BEFORE block edited under the stored hash.
        moved = _moved(existing, entry)
        if moved:
            raise ValueError(f"refusing to update {target}: {_refusal(moved)}")
    _durable_write(target, render_entry(entry), create=not existed)
    return target


class UpdateRefused(Exception):
    """An update refused on the entry as it is on disk (`update_v2`): its
    BEFORE lock is broken, or ``change`` found it no longer applies."""


def update_v2(path: Path, change):
    """Change one v2 entry in place: load it, apply ``change`` (entry -> new
    entry; it may raise `UpdateRefused`) and save it, all under the entry's
    lock.

    A command that loaded the entry earlier and saved its own copy would
    erase whatever another writer saved in between (the review of the Hermes
    fix: a `resolve --commit` beside the sweep's `mark-reported` lost the
    `reported` stamp). ``change`` therefore sees the entry as it is on disk
    now, and must re-check anything it depends on against that entry; the
    BEFORE lock is re-verified here.

    The entry ``change`` returns is verified too (Hermes audit of 424b0b4,
    finding 4): the save compared only the two `before_sha256` fields, so a
    ``change`` that edited the BEFORE block and kept the stored hash wrote an
    entry whose lock no longer verified. What the lock seals (the BEFORE
    block, its hash, when it was locked, when the case was opened) and what
    the file is named for (ticker, day) must come back exactly as read, and
    the lock must verify; otherwise nothing is written. They are compared
    with a copy taken before ``change`` runs, which may have edited the
    entry it was given in place. A ``reported`` stamp may be made (from
    None), never cleared or moved: a case is reported once, and a cleared
    stamp would let its report be generated again after the AFTER block was
    read (the "no peek-then-edit" rule). Returns the saved entry."""
    from app.services.journal.schema_v2 import render_entry, verify_lock

    with _entry_lock(path):
        current = load_v2(path)
        if not verify_lock(current):
            raise UpdateRefused(
                f"LOCK BROKEN — the BEFORE block of {path.name} no longer matches its "
                "hash; refusing to update a tampered entry.")
        read = current.model_copy(deep=True)
        updated = change(current)
        moved = _moved(read, updated)
        if moved:
            raise UpdateRefused(f"{path.name}: {_refusal(moved)}")
        _save_v2_locked(updated, path, True, render_entry)
        return updated


# What an update of a v2 entry must leave exactly as it read it (`update_v2`).
_SEALED = ("before", "before_sha256", "locked_at", "opened", "ticker", "day")


def _moved(read, updated) -> list[str]:
    """What ``updated`` changed of ``read`` that no update may: the sealed
    fields, a `reported` stamp already made, or (named "the BEFORE block")
    anything that leaves its lock unverified. Empty when it may be saved.
    Shared by `update_v2` and `save_v2`'s update, so neither is a way round
    the other."""
    from app.services.journal.schema_v2 import verify_lock

    moved = [name for name in _SEALED if getattr(updated, name) != getattr(read, name)]
    if read.reported is not None and updated.reported != read.reported:
        moved.append("reported")
    if not moved and not verify_lock(updated):
        moved.append("the BEFORE block")
    return moved


def _refusal(moved: list[str]) -> str:
    return (f"the update changed {', '.join(moved)}, which an update must leave as it is "
            "(the lock seals it, the file is named for it, or it is a `reported` stamp "
            "already made); refusing to save it.")


def load_v2(path: Path):
    """Read a v2 entry file. Raises ValueError on a v1 entry (mixed cases should
    dispatch via is_v2 first)."""
    from app.services.journal.schema_v2 import parse_entry

    return parse_entry(path.read_text(encoding="utf-8"))


# Brier / calibration threshold from VALIDATION_STRATEGY §4 (Murphy decomposition
# floor): below this many resolved p_outcome samples, report raw tallies only —
# no calibration claim.
BRIER_MIN_N = 50


def v2_tally(today_iso: str | None = None) -> dict:
    """Aggregate stats for v2 entries. v1 entries are ignored here (they're
    tallied by the v1 `tally()` above).

    Round-10 hardening:
      - **finding 2**: broken/unlocked entries are counted for AUDIT
        (`total`, `lock_broken`) but EXCLUDED from every inferential metric
        (resolutions, open queue, overdue queue, Brier). A tampered
        preregistration cannot be evidence.
      - **finding 3**: Brier scores `p_outcome` against `OutcomeBlock.y` (the
        observed binary outcome the user preregistered), NOT the AFTER
        `verdict` (which scored engine usefulness). Also requires
        `before.outcome_definition` — a Brier without a preregistered event
        definition is not a probability score.
      - **finding 4**: `pending` resolutions don't count as terminal; only
        met/violated/unresolvable populate the resolutions histogram.

    Fields:
      total, locked, lock_broken, resolutions{met,violated,unresolvable,pending},
      open_assumptions, overdue_assumptions (past resolve_by, unresolved),
      resolved_with_p_outcome, brier (None below the Murphy floor or without y).
    """
    import sys as _sys  # local import — do not couple module load to stderr
    from datetime import date as _date

    from app.services.journal.schema_v2 import (
        add_resolution,  # noqa: F401 - kept exported through this module
        open_assumption_indices,
        verify_lock,
    )

    today = _date.fromisoformat(today_iso) if today_iso else _date.today()
    v2_paths = [p for p in list_entries() if is_v2(p)]

    # Round-12 finding 2: a single corrupt v2 file used to crash the whole
    # `tally` command via unhandled ValidationError. Now every file is loaded
    # individually and parse failures are counted, not fatal.
    entries = []
    parse_failed: list[str] = []
    for p in v2_paths:
        try:
            entries.append(load_v2(p))
        except Exception as e:  # noqa: BLE001 - a bad file must not kill the aggregate
            parse_failed.append(p.name)
            print(f"warning: v2 entry {p.name} could not be parsed ({e})", file=_sys.stderr)

    total = len(entries)
    locked = sum(1 for e in entries if verify_lock(e))
    lock_broken = sum(1 for e in entries if e.before_sha256 and not verify_lock(e))
    # Only legitimately-locked entries contribute to inferential metrics.
    trusted = [e for e in entries if verify_lock(e)]

    res_counts = {"met": 0, "violated": 0, "unresolvable": 0, "pending": 0}
    open_assumptions = 0
    overdue: list[tuple[str, str, str]] = []  # (ticker, day, metric)
    for e in trusted:
        for r in e.resolutions:
            res_counts[r.state] = res_counts.get(r.state, 0) + 1
        for i in open_assumption_indices(e):
            open_assumptions += 1
            a = e.before.assumptions[i]
            if a.resolve_by <= today:
                overdue.append((e.ticker, e.day.isoformat(), a.metric))

    # Brier: preregistered probability of a preregistered binary event, scored
    # against the observed y. A user must have supplied BOTH
    # `outcome_definition` (BEFORE) and `y` (OUTCOME) — otherwise the Brier
    # score has no defined target and we withhold it.
    pairs: list[tuple[float, int]] = []
    for e in trusted:
        if not e.before.outcome_definition:
            continue
        p, y = e.before.p_outcome, e.outcome.y
        if p is None or y is None:
            continue
        pairs.append((p, 1 if y else 0))

    brier = (
        sum((p - y) ** 2 for p, y in pairs) / len(pairs)
        if len(pairs) >= BRIER_MIN_N
        else None
    )

    return {
        "total": total,
        "locked": locked,
        "lock_broken": lock_broken,
        "parse_failed": parse_failed,
        "resolutions": res_counts,
        "open_assumptions": open_assumptions,
        "overdue_assumptions": overdue,
        "resolved_with_p_outcome": len(pairs),
        "brier": brier,
        "brier_min_n": BRIER_MIN_N,
    }


def tally() -> dict:
    # v2 entries are counted separately by `v2_tally()`; the v1 markdown parser
    # can't read a JSON front-matter file (would yield an entry with all fields
    # None and quietly inflate v1 stats).
    # A file that cannot be read (permissions, a bad mount) used to take the
    # whole tally — and with it the dashboard — down with an OSError. One
    # broken file is not a reason to lose the count of every other case;
    # `v2_tally` already works this way. They are counted, never silent.
    entries, unreadable = [], []
    for p in list_entries():
        if is_v2(p):
            continue
        try:
            entries.append(parse_entry(p))
        except OSError:
            unreadable.append(p.name)
    total = len(entries)
    scored = [e for e in entries if e["impact"]]
    impact_counts = {code: sum(1 for e in scored if code in (e["impact"] or "")) for code in IMPACT_CODES}
    verdicts: dict[str, int] = {}
    conv_moved = conv_same = 0
    for e in entries:
        if e["verdict"]:
            verdicts[e["verdict"]] = verdicts.get(e["verdict"], 0) + 1
        cb, ca = e["conviction"], e["conviction_after"]
        if cb and ca and cb.isdigit() and ca.isdigit():
            conv_moved += int(cb) != int(ca)
            conv_same += int(cb) == int(ca)
    changed_any = len(scored) - impact_counts["no_value"] if scored else 0
    # AFTER already filled but no verdict yet, oldest first — the "don't forget to
    # close this out" queue. Must require `impact` too: a reported case whose AFTER
    # block is still blank needs THAT step first, not an outcome (needs_outcome
    # alone is true for both states and would conflate them).
    awaiting_outcome = sorted(
        (e for e in entries if e["is_reported"] and e["impact"] and e["needs_outcome"]),
        key=lambda e: e["days_since_reported"] or 0,
        reverse=True,
    )
    return {
        "entries": entries,
        "total": total,
        "scored": len(scored),
        "with_outcome": sum(1 for e in entries if e["verdict"]),
        "impact_counts": impact_counts,
        "changed_any": changed_any,
        "conv_moved": conv_moved,
        "conv_same": conv_same,
        "verdicts": verdicts,
        "awaiting_outcome": awaiting_outcome,
        "oldest_awaiting_days": awaiting_outcome[0]["days_since_reported"] if awaiting_outcome else None,
        "stale_outcome_days": STALE_OUTCOME_DAYS,
        "gate_ready": total >= 20 and sum(1 for e in entries if e["verdict"]) >= 15,
        "unreadable": unreadable,
    }
