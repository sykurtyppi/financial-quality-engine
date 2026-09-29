"""Append-only companyfacts vintage store, and the diff over it (P1-F Tier 2).

`detect_restatements` recovers a revision WITHIN one companyfacts document:
when two filings both present the same period, the difference is visible
today. The quieter case leaves nothing to compare — a company revises a prior
figure and simply does not re-present the original, so companyfacts holds only
the new value and the change is invisible from any single fetch, forever.

The only way to see it is to have kept what the number used to be. So this
stores the raw document, unmodified, each time it changes, and diffs one
vintage against another. Nothing here can be back-filled FROM COMPANYFACTS:
it is a live view, and a value it no longer carries cannot be asked for. SEC
DERA's quarterly Financial Statement Data Sets publish numeric facts as
filed, so an as-filed history may be reconstructible from them — a separate
project, at quarterly granularity and with a publication lag, not a
substitute for a daily snapshot taken now. That is why capture runs from the
day it lands rather than waiting for the surfacing work.

Layout (gitignored — it is bulk source data, ~0.3 MB gzipped per snapshot):

    data/vintages/CIK##########/manifest.json
    data/vintages/CIK##########/<YYYY-MM-DD>-<sha12>.json.gz

A snapshot is named for its CONTENT, not just the day: two captures on one
day that differ are two different documents, and the earlier one is exactly
what a mid-day revision would otherwise erase. Identical content resolves to
the same name and is written once.

The manifest indexes the snapshots and records the last date the source was
checked at all — so an unchanged document costs one fetch a day, not one an
hour, and "we looked and it was identical" stays distinguishable from "we
never looked". The files on disk are the record; the manifest is rebuilt from
them if it is ever lost.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.services.ingestion.companyfacts_mapper import (
    FLOW_FIELDS,
    INSTANT_FIELDS,
    IngestionDiagnostics,
    _parse_date,
    _unit_for,
    build_dataset,
)
from app.services.ingestion.fields import FIELDS
from app.services.ingestion.payloads import ExternalPayloadError
from app.services.ingestion.precedence import rank
from app.services.ingestion.restatements import (
    DEFAULT_MATERIALITY_PCT,
    SPLIT_ADJUSTED_FIELDS,
    _active_tag,
    _rows,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
VINTAGES = ROOT / "data" / "vintages"
MANIFEST = "manifest.json"
LOCK = ".lock"
BUSY_PREFIX = ".busy-"
LOCK_TIMEOUT_S = 30.0
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-([0-9a-f]{6,64}))?(?:-\d+)?\.json\.gz$")
# The materiality floor is the within-snapshot detector's, imported rather
# than restated: the two tiers describe the same thing and a second copy
# would drift apart unnoticed.
# What reading a snapshot can raise when the file is not a readable archive.
# gzip raises EOFError on a truncated member — which subclasses neither
# OSError nor ValueError, so it escapes the obvious handler and would reach
# the CLI as a traceback and the sweep as a lost pass.
UNREADABLE = (OSError, ValueError, EOFError)
# What reading AND mapping a stored snapshot can raise when it is not usable
# as a state: unreadable, or read but built from a shape the mapper does not
# expect. The SEC shape check passes `{"facts": {"us-gaap": []}}` or a null
# `units`, and the mapper's `.get` chain then raises AttributeError, not the
# ValueError `_mapped` absorbs. Only where a stored state is being CHOSEN
# (the report's legacy walk and thesis-lock capture, the replay's pick) and
# nothing says a report scored it (a raw or legacy state). A SCORED payload
# was built by the mapper already: a TypeError there is a mapper defect and
# must surface, so it catches UNREADABLE alone (review of c131583, finding
# 3). Wherever a state is passed over, the reason is logged.
UNUSABLE = (*UNREADABLE, TypeError, AttributeError, KeyError)
# What wrote an observation (Hermes audit of 424b0b4, finding 2). SCORED: the
# payload a report scored (`store_snapshot`, reached only after the mapper
# built it). RAW: a fetch archived as it came (`capture` — the watch sweep,
# `scripts/vintage.py capture`), never mapped and possibly partial: a bare
# `{"cik", "entityName"}` passes the SEC shape check. The report's
# silent-revision baseline is a scored state; a raw one is kept for the CLI.
SCORED = "scored"
RAW = "raw"


def cik_dir(cik: int, root: Path | None = None) -> Path:
    return (root or VINTAGES) / f"CIK{int(cik):010d}"


def _manifest_path(cik: int, root: Path | None = None) -> Path:
    return cik_dir(cik, root) / MANIFEST


def canonical_bytes(facts: dict) -> bytes:
    """The bytes a snapshot's digest is taken over. Canonical, so the same
    document always hashes the same way however it arrived."""
    return json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()


def digest_of(facts: dict) -> str:
    return hashlib.sha256(canonical_bytes(facts)).hexdigest()


def snapshot_name(day: date, digest: str) -> str:
    return f"{day.isoformat()}-{digest[:12]}.json.gz"


def snapshot_day(path: Path) -> str:
    """The capture date out of a snapshot filename. Not `name.split(".")[0]`:
    a content-addressed name carries the digest too."""
    m = _NAME_RE.match(path.name)
    return m.group(1) if m else path.name.split(".")[0]


def _sweep_orphans(d: Path, older_than_s: float = 3600.0) -> None:
    """Remove temp files a hard kill left behind. Only ours, only stale, and
    only under the lock — a partial write is never a snapshot, but it should
    not accumulate either."""
    cutoff = time.time() - older_than_s
    for p in d.glob(".*.tmp"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _record_problem_day(cik: int, day: date, root: Path | None = None) -> None:
    """Record that this DAY archived nothing, without touching lock-protected
    state.

    One marker per calendar day, whatever went wrong. Counting distinct days
    is the whole point: an hourly job meeting a wedged lock would otherwise
    report twenty-four "problem days" before lunch, and two problem days of
    different kinds — a failed write, then a lock timeout — would report as
    one if each kind kept its own tally.
    """
    try:
        d = cik_dir(cik, root)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{BUSY_PREFIX}{day.isoformat()}").touch(exist_ok=True)
    except OSError:
        pass


def _problem_days(cik: int, root: Path | None = None) -> int:
    try:
        return sum(1 for path in cik_dir(cik, root).glob(f"{BUSY_PREFIX}*") if path.is_file())
    except OSError:
        return 0


def _clear_problem_days(cik: int, root: Path | None = None) -> None:
    for path in cik_dir(cik, root).glob(f"{BUSY_PREFIX}*"):
        try:
            path.unlink()
        except OSError:
            pass


def _free_path(d: Path, day: date, sha: str) -> Path:
    """Where this document goes. The name carries a 48-bit prefix of the
    digest, ample to tell a day's documents apart — but a collision would mean
    writing over a snapshot that is NOT this content, so it is checked rather
    than assumed. Costs a hash only when a name is already taken."""
    out = d / snapshot_name(day, sha)
    n = 1
    while out.exists():
        try:
            if digest_of(load_vintage(out)) == sha:
                return out  # same content, same name: nothing to disambiguate
        except UNREADABLE:
            pass  # unreadable: do not trust it, and never write over it
        out = d / f"{day.isoformat()}-{sha[:12]}-{n}.json.gz"
        n += 1
    return out


@contextmanager
def _cik_lock(cik: int, root: Path | None = None, timeout: float | None = None):
    """One capture at a time per company. The manifest is a read-modify-write
    and the sweep is not the only writer — `vintage.py capture` is a
    documented manual command — so without this two runs can each build a
    manifest from the same stale read and the last one erases the other's
    snapshot record. Yields False if the lock cannot be taken in time; the
    caller then does nothing, which is the safe outcome for an archive."""
    # Read at call time, never bound as a default: a default freezes the
    # module constant at import and cannot be tuned or tested.
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    d = cik_dir(cik, root)
    d.mkdir(parents=True, exist_ok=True)
    give_up = time.monotonic() + max(timeout, 0.0)
    with (d / LOCK).open("w") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= give_up:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _entry_for(path: Path) -> dict:
    """A manifest entry rebuilt from a file on disk.

    `sha256` is always the FULL digest of the content or an empty string —
    never the truncated prefix the filename carries. A short value in a field
    named sha256 is a lie the "do we already hold this?" check would have to
    reason around; an empty one says plainly that the file is there and
    cannot be read.
    """
    m = _NAME_RE.match(path.name)
    try:
        sha = digest_of(load_vintage(path))
    except UNREADABLE:
        sha = ""
    return {"captured": m.group(1) if m else "", "sha256": sha,
            "file": path.name, "bytes": path.stat().st_size}


def reconcile_manifest(cik: int, root: Path | None = None,
                       skip: set[str] | None = None) -> list[dict]:
    """Snapshot entries rebuilt from the files on disk. The manifest is an
    index, never the archive: losing it must not make capture think there is
    no history and start again.

    `skip` names files the caller already has an entry for. Rebuilding one
    entry means decompressing and re-hashing a multi-megabyte document, and
    capture reads the manifest on every pass of an hourly job — so an index
    that is already correct must cost nothing.
    """
    skip = skip or set()
    return [_entry_for(p) for p in list_vintages(cik, root) if p.name not in skip]


def read_manifest(cik: int, root: Path | None = None) -> dict:
    """{"last_checked", "problem_days", "snapshots": [{captured, sha256, file, bytes}]}.

    A missing or unreadable manifest is rebuilt from the snapshots on disk, so
    a corrupt index can never erase the history that capture compares against.
    """
    try:
        data = json.loads(_manifest_path(cik, root).read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"last_checked": None, "problem_days": _problem_days(cik, root),
                "observations": [],
                "snapshots": reconcile_manifest(cik, root)}
    data.setdefault("last_checked", None)
    data.setdefault("problem_days", 0)
    data.setdefault("observations", [])
    # The markers are the source of truth — one per distinct bad day. `max`
    # only honours a count stored before markers existed; both are cleared
    # together by any success, so a legacy value cannot outlive its store.
    data["problem_days"] = max(
        int(data.get("problem_days") or 0), _problem_days(cik, root)
    )
    # An entry with no digest is kept: it records a file we hold but cannot
    # read, which is exactly the thing an operator needs to see.
    data["snapshots"] = [s for s in data["snapshots"]
                         if isinstance(s, dict) and s.get("file")]
    known = {s["file"] for s in data["snapshots"]}
    data["snapshots"] += reconcile_manifest(cik, root, skip=known)
    data["snapshots"].sort(key=lambda s: (s.get("captured", ""), s.get("file", "")))
    return data


def _observe(man: dict, day: date, sha: str, kind: str) -> None:
    """Record WHICH document was live on which day, and what wrote it.

    Content is stored once, so a document that goes A, then B, then back to A
    leaves two files and no way to tell B was ever live — the archive would
    imply A had stood the whole time. The snapshots are the data; this is the
    order they were seen in, and it cannot be reconstructed later.

    `kind` is SCORED or RAW. A repeat of the last observation records nothing
    new but may upgrade it: the sweep fetched the document, then a report
    scored the identical payload. Never the other way — content a report
    scored stays scored however often the sweep fetches it again
    (`observed_vintages` reads the kind per content).
    """
    obs = man.setdefault("observations", [])
    if obs and obs[-1].get("sha256") == sha and obs[-1].get("date") == day.isoformat():
        if kind == SCORED:
            obs[-1]["kind"] = SCORED
        return
    obs.append({"date": day.isoformat(), "sha256": sha, "kind": kind})


def _last_seen(man: dict) -> str:
    """The document as it stood at the last check: the last observation.
    Not the manifest's last snapshot, which is ordered by (captured, file):
    two snapshots taken on one day sort by content hash, not by when they
    were seen. A store older than observations falls back to that order,
    the only one it has."""
    observed = man.get("observations") or []
    if observed and observed[-1].get("sha256"):
        return str(observed[-1]["sha256"])
    return man["snapshots"][-1]["sha256"] if man["snapshots"] else ""


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # a rename is atomic; the bytes must be there first
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Capture:
    """What one capture attempt did. `path` is None when nothing was written."""

    cik: int
    checked: date
    path: Path | None
    sha256: str
    reason: str  # captured | unchanged | already checked today | busy | failed
    detail: str = ""

    @property
    def wrote(self) -> bool:
        return self.path is not None

    @property
    def problem(self) -> bool:
        """True when this pass archived nothing for a reason that needs a
        human eventually — a wedged lock or a failed write, not the ordinary
        "nothing changed"."""
        return self.reason in ("busy", "failed")

    def describe(self) -> str:
        """One line for a report's data-quality appendix. Says what happened
        to the baseline the silent-revision check depends on — never silent
        about a failure, never alarming about an ordinary no-op."""
        if self.reason == "captured" and self.path is not None:
            try:
                size = f" ({self.path.stat().st_size / 1024:.0f} KB)"
            except OSError:
                size = ""
            return f"captured {self.path.name}{size}"
        if self.reason == "unchanged":
            return f"unchanged since the last snapshot (sha {self.sha256[:12]})"
        if self.reason == "reverted":
            return f"reverted {self.detail}"
        if self.reason == "already checked today":
            return "already checked today; no new snapshot"
        return f"NOT captured ({self.reason}): {self.detail}"


def capture(
    client,
    ticker: str,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    force: bool = False,
) -> Capture:
    """Snapshot `ticker`'s companyfacts if it has changed since the last one.

    Once a day per name unless `force`: the document changes on filing days,
    not hourly, and the sweep calls this on every pass. Identical content is
    never stored twice, and DIFFERENT content is never stored over — a second
    capture on a day a filing landed is a second file, because the earlier one
    is precisely what the revision would otherwise erase.

    The fetch stays BEHIND the daily gate: a name already checked today costs
    no request. Callers that already hold the payload use `store_snapshot`.
    """
    cik = client.resolve_cik(ticker)
    return _store(cik, lambda: client.company_facts_by_cik(cik), now=now, root=root,
                  force=force, kind=RAW)


def store_snapshot(
    cik: int,
    facts: dict,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    force: bool = False,
) -> Capture:
    """Archive a companyfacts payload the caller has ALREADY fetched.

    The report entry points hold the exact document the engine scored
    (`DatasetSnapshot.company_facts`) and were throwing it away; this stores
    that document, so the snapshot on disk is byte-for-byte what was scored
    rather than a second fetch that may straddle a filing. Same gate, same
    manifest, same content addressing as `capture` — a payload that
    `capture` would have fetched produces the identical file and digest.

    NOT gated by the once-a-day check. That gate exists to save a fetch;
    this payload is already in hand, and it is the one the report scored.
    Gating it meant a morning `capture` (the watch sweep, the daily
    portfolio run) made that evening's filing-night report drop the payload
    that carried the filing — and the silent-revision section then compared
    two older snapshots and said nothing had changed. Content addressing
    still stores an identical payload only once. (`force` is kept for API
    compatibility; it has nothing left to override.)

    Recorded as SCORED: this is the state the report's silent-revision
    baseline may be taken from; a `capture` of the same content is upgraded.
    """
    return _store(cik, lambda: facts, now=now, root=root, force=True, kind=SCORED)


def _store(
    cik: int,
    load,
    *,
    now: datetime | None,
    root: Path | None,
    force: bool,
    kind: str,
) -> Capture:
    """The lock / daily-gate / dedupe / atomic-write core shared by `capture`
    and `store_snapshot`. `load` is invoked only once the gate has decided a
    document is actually needed; `kind` is what the observation records."""
    # The LOCAL calendar day, however the instant is expressed. Reports date
    # themselves with the local `date.today()` and read the store "as of"
    # that day; stamping snapshots in UTC put an evening (US) capture on
    # tomorrow's date, invisible to the report that made it. An aware
    # instant (the watch sweep passes UTC) is converted; a naive one is
    # taken as local.
    today = (now or datetime.now(UTC)).astimezone().date()
    with _cik_lock(cik, root) as held:
        if not held:
            # Never touch the manifest without its lock. The process holding
            # the lock may be between its own read and atomic replace; even a
            # well-formed write here can restore stale state over that update.
            _record_problem_day(cik, today, root)
            return Capture(cik, today, None, "", "busy",
                           f"another capture holds the lock ({cik_dir(cik, root) / LOCK})")
        _sweep_orphans(cik_dir(cik, root))
        man = read_manifest(cik, root)
        if not force and man.get("last_checked") == today.isoformat():
            newest = _last_seen(man)
            if _problem_days(cik, root):
                # Today's attempt archived nothing (any success today would
                # have cleared the marker). The gate stays closed — a broken
                # disk must not turn one fetch a day into one an hour — but
                # it must not erase the marker either: that delayed the
                # VINTAGE_STALE_DAYS operator alert and told the report the
                # day was fine.
                return Capture(cik, today, None, newest, "failed",
                               "an earlier attempt today archived nothing; "
                               "not retried until tomorrow")
            return Capture(cik, today, None, newest, "already checked today")

        facts = load()
        raw = canonical_bytes(facts)
        sha = hashlib.sha256(raw).hexdigest()
        man["last_checked"] = today.isoformat()
        known = {s.get("sha256") for s in man["snapshots"]}
        observed = man.get("observations") or []
        last_seen = observed[-1].get("sha256") if observed else None
        _observe(man, today, sha, kind)
        if sha in known:
            man["problem_days"] = 0
            _write_json_atomic(_manifest_path(cik, root), man)
            _clear_problem_days(cik, root)
            if last_seen is not None and last_seen != sha:
                # Stored before, but NOT what was live last time: the filer
                # went back to an earlier state. That is a change, and the
                # one a silent-revision baseline most needs to name.
                return Capture(cik, today, None, sha, "reverted",
                               f"back to an earlier snapshot (sha {sha[:12]}); "
                               f"the last one observed was {last_seen[:12]}")
            return Capture(cik, today, None, sha, "unchanged")

        # Record that today's fetch HAPPENED before attempting the larger,
        # more failure-prone archive write. Otherwise a failing write leaves
        # `last_checked` unset and the daily gate never closes, turning one
        # multi-megabyte fetch a day into one an hour for as long as the
        # disk stays broken — while archiving nothing either way.
        try:
            _write_json_atomic(_manifest_path(cik, root), man)
        except OSError:
            pass  # the write below will fail too and is the one that matters

        out = _free_path(cik_dir(cik, root), today, sha)
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        try:
            # mtime=0: the archive bytes depend only on the content, so an
            # unchanged document cannot look changed to anything comparing files.
            with tmp.open("wb") as fobj:
                with gzip.GzipFile(filename="", mode="wb", fileobj=fobj, mtime=0) as fh:
                    fh.write(raw)
                fobj.flush()
                os.fsync(fobj.fileno())  # survive a power loss, not just a kill
            os.replace(tmp, out)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            # A marker, not an increment: a failed write and a lock timeout
            # are both "this day archived nothing", and counting them in two
            # separate places made two bad days report as one — delaying the
            # operator alert that fires at VINTAGE_STALE_DAYS.
            _record_problem_day(cik, today, root)
            man["problem_days"] = _problem_days(cik, root)
            try:
                _write_json_atomic(_manifest_path(cik, root), man)
            except OSError:
                pass
            return Capture(cik, today, None, sha, "failed", f"{type(e).__name__}: {e}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # Append-only: an entry is added, never replaced. Two snapshots on one
        # day are two entries.
        man["snapshots"] = [s for s in man["snapshots"] if s.get("file") != out.name]
        man["snapshots"].append(_entry_for(out))
        man["snapshots"].sort(key=lambda s: (s.get("captured", ""), s.get("file", "")))
        man["problem_days"] = 0
        _write_json_atomic(_manifest_path(cik, root), man)
        _clear_problem_days(cik, root)
        return Capture(cik, today, out, sha, "captured")


def list_vintages(cik: int, root: Path | None = None) -> list[Path]:
    """Snapshots oldest first, taken from disk rather than the manifest so a
    lost index never hides data that is still there."""
    d = cik_dir(cik, root)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json.gz") if not p.name.startswith("."))


@dataclass(frozen=True)
class VintageObservation:
    """One distinct content state in the order it was observed.

    `kind`: SCORED when a report scored this content (on any observation of
    it), RAW when only `capture` ever recorded it, None when the store
    predates kinds (or lost its manifest) and nothing says — `report_diff`
    then decides by whether the snapshot maps."""

    captured: str
    sha256: str
    path: Path
    kind: str | None = None


def observed_vintages(cik: int, root: Path | None = None) -> list[VintageObservation]:
    """Distinct content states in true observation order.

    Filenames are content-addressed, so sorting two same-day files sorts by
    hash, not capture time. The manifest's append-only observations are the
    only durable ordering record. Consecutive unchanged observations collapse;
    a later reversion (A -> B -> A) remains a third state even though it reuses
    A's existing file.

    Old stores without observations fall back to disk order. That preserves
    compatibility, but same-day order from before observations existed cannot
    be reconstructed.
    """
    paths = list_vintages(cik, root)
    if not paths:
        return []
    man = read_manifest(cik, root)
    by_sha: dict[str, Path] = {}
    sha_by_name: dict[str, str] = {}
    # Kind is a property of the CONTENT: a document a report scored is
    # scored on every day it was observed, and a sweep fetching it again
    # never downgrades it. An observation written before kinds existed
    # leaves its content unknown rather than raw — it may well have been a
    # report's payload.
    seen: dict[str | None, set[str | None]] = {}
    for observation in man.get("observations", []):
        seen.setdefault(observation.get("sha256"), set()).add(observation.get("kind"))

    def kind_of(sha: str | None) -> str | None:
        kinds = seen.get(sha, set())
        if SCORED in kinds:
            return SCORED
        return RAW if kinds == {RAW} else None

    for entry in man.get("snapshots", []):
        sha, name = entry.get("sha256"), entry.get("file")
        if name:
            sha_by_name[name] = sha or ""
        if sha and name:
            path = cik_dir(cik, root) / name
            if path.is_file():
                by_sha[sha] = path

    out: list[VintageObservation] = []
    for observation in man.get("observations", []):
        sha = observation.get("sha256")
        path = by_sha.get(sha)
        captured = observation.get("date")
        if not path or not captured or (out and out[-1].sha256 == sha):
            continue
        out.append(VintageObservation(captured, sha, path, kind_of(sha)))

    if not out:
        return [
            VintageObservation(snapshot_day(path), sha_by_name.get(path.name, ""), path,
                               kind_of(sha_by_name.get(path.name)))
            for path in paths
        ]

    # A crash after the archive rename but before the final manifest replace
    # can leave a valid file without an observation. Keep it visible, using
    # the only ordering information still available.
    represented = {item.path for item in out}
    for path in paths:
        if path not in represented:
            out.append(VintageObservation(
                snapshot_day(path), sha_by_name.get(path.name, ""), path,
                kind_of(sha_by_name.get(path.name)),
            ))
    out.sort(key=lambda item: item.captured)  # stable: observed same-day order is preserved
    return out


def load_vintage(path: Path) -> dict:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


# --------------------------------------------------------------------------
# Diff


@dataclass(frozen=True)
class FactKey:
    taxonomy: str
    tag: str
    unit: str
    start: date | None
    end: date

    @property
    def period(self) -> str:
        return f"{self.start} → {self.end}" if self.start else str(self.end)


@dataclass(frozen=True)
class VintageChange:
    """One prior-period figure that moved between two snapshots.

    `kind` is `revised` (the scored value changed) or `withdrawn` (the fact is
    gone from the later document). Both are invisible to the within-snapshot
    detector when the filer does not re-present the original.

    `original_retained`: the newer snapshot still carries the fact the older
    value was read from (same accession, period and value) beside the later
    filing the value now comes from. The within-snapshot detector reads that
    pair from filing history, so the move is not silent
    (`explained_by_filing`): an amendment landing is the ordinary case.
    """

    kind: str
    field_name: str
    key: FactKey
    old_value: float
    old_filed: date | None
    old_accession: str
    old_form: str
    new_value: float | None = None
    new_filed: date | None = None
    new_accession: str = ""
    new_form: str = ""
    pct_change: float | None = None
    new_tag: str = ""  # set when the filer moved the field to another XBRL tag
    # "scored": a figure the engine scores, compared as it scores it.
    # "context": a raw fact of a scored tag for a period older than the
    # reported window — shown, never promoted.
    # "raw": a raw fact row returned because a snapshot could not be mapped
    # (`diff_scored`'s fallback) — not a change to a scored figure; shown,
    # never promoted or validated.
    scope: str = "scored"
    original_retained: bool = False

    @property
    def moved_tag(self) -> bool:
        return bool(self.new_tag) and self.new_tag != self.key.tag

    @property
    def explained_by_filing(self) -> bool:
        """Moved with a later filing that the newer snapshot carries beside
        the original — visible in filing history, so not a silent revision."""
        return (self.kind == "revised" and self.original_retained
                and bool(self.new_accession) and self.new_accession != self.old_accession)


def _retains(older: dict, newer: dict, key: FactKey, accession: str) -> bool:
    """Whether `newer` still carries the fact `older` holds under `accession`
    for `key`'s concept and period, at the same value."""
    def values(facts: dict) -> set[float]:
        out: set[float] = set()
        for row in _rows(facts, key.taxonomy, key.tag, key.unit):
            try:
                if row.get("accn") != accession or _parse_date(row["end"]) != key.end:
                    continue
                if (_parse_date(row["start"]) if row.get("start") else None) != key.start:
                    continue
                out.add(float(row["val"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    return bool(accession) and bool(values(older) & values(newer))


def _scored_tags(include_split_adjusted: bool = False) -> dict[str, tuple]:
    """field_name -> (candidate tags, unit) for every field the engine scores.

    Share counts are excluded by default, for the reason the within-snapshot
    detector excludes them: a stock split retroactively rewrites every prior
    share count, which is a corporate action and not a revision. Measured on
    the first real capture — NVDA's June 2024 ten-for-one split produced four
    "revisions" of exactly 900%, and they were the only findings in the file.
    """
    out: dict[str, tuple] = {}
    for field_name, candidates in {**INSTANT_FIELDS, **FLOW_FIELDS}.items():
        if not include_split_adjusted and field_name in SPLIT_ADJUSTED_FIELDS:
            continue
        out[field_name] = (candidates, _unit_for(field_name))
    return out


def _series(facts: dict, scored_only: bool,
            include_split_adjusted: bool = False) -> dict[tuple, dict]:
    """(field, start, end) -> the value that period currently stands at.

    Keyed by FIELD, not by tag. A filer that moves a field to another XBRL
    tag has not changed the number, and keying by tag would read the move as
    a disappearance and an appearance — the sibling detector's docstring is
    explicit that comparing across tags fabricates a restatement at every
    taxonomy change. Keying by field compares like with like.

    Within a snapshot the tag is chosen the way the engine chooses it —
    `_active_tag`, best coverage — so a revision reported here is a revision
    to the number a report would show, not to some abandoned candidate tag
    carrying stale data. The unit is the field's unit, with the same
    mis-filed-share-count fallback the sibling detector uses. One concept
    per field: a quarter the mapper fills from another concept after a tag
    switch is not followed here (`raw_diff_blind_spots` names it;
    `diff_scored` compares it as scored).

    Same-day ties resolve as the mapper's `_dedupe_latest_filed` does, by
    the shared `precedence` order (an amendment over an original, then the
    higher accession); a full tie keeps the FIRST row in companyfacts order
    (`>`, not `>=`). Round-8 of the sibling detector was a bug where
    sorting and taking the last diverged from scoring; do not "tidy" this
    into a sort.
    """
    out: dict[tuple, dict] = {}

    def _take(field: str, taxonomy: str, tag: str, unit: str) -> None:
        for row in _rows(facts, taxonomy, tag, unit):
            try:
                end = _parse_date(row["end"])
                filed = _parse_date(row["filed"])
                val = float(row["val"])
                start = _parse_date(row["start"]) if row.get("start") else None
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(val):
                # Not a reported number: the mapper drops it (`_collect`), so
                # no report shows it and it is no revision here (review of
                # deb6364, finding 1).
                continue
            k = (field, start, end)
            prev = out.get(k)
            form = row.get("form", "")
            accn = row.get("accn", "")
            if prev is None or rank(filed, form, accn) > rank(prev["filed"], prev["form"], prev["accn"]):
                out[k] = {"filed": filed, "val": val, "accn": accn, "form": form,
                          "key": FactKey(taxonomy, tag, unit, start, end)}

    if scored_only:
        for field, (candidates, unit) in _scored_tags(include_split_adjusted).items():
            active = _active_tag(facts, candidates, unit)
            if active is not None:
                _take(field, active[0], active[1], unit)
        return out

    # Every tag, each its own series: an unfiltered view for a human asking
    # what moved anywhere, not the engine's view of a company.
    for taxonomy, tags in (facts.get("facts") or {}).items():
        if not isinstance(tags, dict):
            continue
        for tag, concept in tags.items():
            for unit in ((concept or {}).get("units") or {}):
                _take(f"{taxonomy}:{tag}", taxonomy, tag, unit)
    return out


def diff_vintages(
    older: dict,
    newer: dict,
    *,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    scored_only: bool = True,
    since: date | None = None,
    include_split_adjusted: bool = False,
) -> list[VintageChange]:
    """Prior-period figures that changed or vanished between two snapshots.

    Facts ADDED are deliberately not reported: an ordinary new filing adds
    facts for new periods, which is not a revision, and telling a genuine
    back-fill from that needs a period-age rule there is no data to
    calibrate yet. Revisions and withdrawals are the blind spot this store
    exists for.

    Share counts are excluded unless `include_split_adjusted`: a split is not
    a restatement.

    Raw facts, one concept per field (`_series`): a quarter the mapper
    filled from another concept is not inspected. A caller that shows this
    diff on its own must print `raw_diff_blind_spots` beside it.
    """
    a = _series(older, scored_only, include_split_adjusted)
    b = _series(newer, scored_only, include_split_adjusted)
    changes: list[VintageChange] = []
    for k, old in a.items():
        field_name, _start, end = k
        if since is not None and end < since:
            continue
        new = b.get(k)
        if new is None:
            changes.append(VintageChange(
                "withdrawn", field_name, old["key"], old["val"], old["filed"],
                old.get("accn", ""), old.get("form", "")))
            continue
        if new["val"] == old["val"]:
            continue
        pct = None if old["val"] == 0 else abs(new["val"] - old["val"]) / abs(old["val"])
        if pct is not None and pct < materiality_pct:
            continue
        old_accn = old.get("accn", "")
        changes.append(VintageChange(
            "revised", field_name, old["key"], old["val"], old["filed"],
            old_accn, old.get("form", ""), new["val"], new["filed"],
            new.get("accn", ""), new.get("form", ""), pct, new["key"].tag,
            original_retained=(new.get("accn", "") != old_accn
                               and _retains(older, newer, old["key"], old_accn))))
    changes.sort(key=lambda c: (c.key.end, c.field_name, c.key.tag), reverse=True)
    return changes


def raw_diff_blind_spots(*snapshots: dict) -> list[str]:
    """The reported quarters the raw fact diff cannot see, one line each.

    `diff_vintages` follows ONE concept per field (`_active_tag`). The
    mapper can fill a quarter the selected concept does not report from
    another candidate proven equal to it (a filer's tag switch), and that
    quarter's value is then read from a concept the raw diff never
    follows: a revision of it would pass unseen, and "nothing changed"
    would claim coverage it does not have. `diff_scored` compares those
    quarters as the engine builds them; a caller showing the raw diff alone
    prints these lines beside it. A snapshot the mapper cannot build has no
    such quarters."""
    out: list[str] = []
    for facts in snapshots:
        mapped = _mapped_diagnostics(facts)
        if mapped is None:
            continue
        for d in mapped.fields:
            for period_end, concept in sorted(d.fallbacks.items()):
                line = (
                    f"{d.field_name} at {period_end} is read from {concept} (the selected "
                    f"concept, {d.tag_used}, does not report it); the raw fact diff follows "
                    "one concept per field and did not inspect it"
                )
                if line not in out:
                    out.append(line)
    return out


def _mapped_diagnostics(facts: dict) -> IngestionDiagnostics | None:
    try:
        return build_dataset(facts, "SNAPSHOT")[1]
    except ValueError:
        return None


# `FactKey.taxonomy` of a change to a figure no single fact carries (total
# debt, a composite SG&A or D&A, depreciation standing in for D&A): its
# `tag` names what the value was built from.
COMPOSED = "composed"


@dataclass(frozen=True)
class _Mapped:
    """One snapshot as the engine scores it — built by the mapper itself, so
    the same strategies, exclusions, component rules and per-quarter
    resolution: every field's value per reported quarter, how each was built,
    and the reported quarters."""

    values: dict[str, dict[date, float]]
    sources: dict[str, dict[date, tuple[str, tuple[str, ...]]]]
    window: list[date]


def _mapped(facts: dict, as_of: date | None = None) -> _Mapped | None:
    """`facts` as the engine scores it, or None when the mapper cannot build
    it. `as_of`: as a reader on that day could have built it (the mapper's
    one point-in-time path)."""
    try:
        ds, diag = build_dataset(facts, "SNAPSHOT", as_of=as_of)
    except ValueError:
        return None
    values = {
        d.field_name: {
            p.period_end: v for p in ds.periods if (v := getattr(p, d.field_name)) is not None
        }
        for d in diag.fields
    }
    sources = {
        d.field_name: {
            date.fromisoformat(q): (src.strategy, tuple(src.components))
            for q, src in d.period_sources.items()
        }
        for d in diag.fields
    }
    return _Mapped(values, sources, [p.period_end for p in ds.periods])


def _coverage_gaps(capture: _Mapped, reference: _Mapped, since: date | None = None) -> list[str]:
    """What `reference` maps that `capture` does not hold as the engine builds
    it, one "field (N quarter(s) missing, M built from other concepts)" per
    field; empty when `capture` covers it.

    Every scored, non-split field `reference` maps, at every reported quarter
    from `capture`'s first on (an older one is outside anything the lock
    diff reads) and from `since`: `capture` must hold it, built by the same
    strategy from the same concepts — the per-quarter composition
    `diff_scored` compares, which names a fallback concept too. A capture
    that maps but lacks a field or the latest quarters hid a real revision
    (additions are never reported), and one lacking a component read as a
    recomposed "change" of every quarter (review of 626ca1b, finding 1)."""
    start = min(capture.window, default=None)
    gaps: list[str] = []
    for name, by_q in reference.values.items():
        if name in SPLIT_ADJUSTED_FIELDS:
            continue
        missing = recomposed = 0
        for end in by_q:
            if (start is not None and end < start) or (since is not None and end < since):
                continue
            if end not in capture.values.get(name, {}):
                missing += 1
            elif capture.sources.get(name, {}).get(end) != reference.sources.get(name, {}).get(end):
                recomposed += 1
        parts = [f"{missing} quarter(s) missing" if missing else "",
                 f"{recomposed} built from other concepts" if recomposed else ""]
        if missing or recomposed:
            gaps.append(f"{name} ({', '.join(p for p in parts if p)})")
    return gaps


def _bare(components: tuple[str, ...]) -> str:
    return "+".join(c.split(":", 1)[-1] for c in components)


@dataclass(frozen=True)
class ScoredDiff:
    """Every scored figure that moved between two snapshots, compared as the
    engine scores it. `canonical_unavailable` says why that comparison could
    not be made (a snapshot the mapper cannot build) — the changes are then
    the raw fact diff alone — so silence never reads as "nothing moved"."""

    changes: list[VintageChange]
    canonical_unavailable: str | None = None


# `diff_scored`'s reason when a side cannot be mapped is built from these, so
# the report can say the short "why" where the window is already named
# (`_why_not_scored`) without restating the prefix.
_NOT_SCORED = "scored values not compared as the engine builds them: "
_RAW_ONLY = " (raw fact rows only, not scored changes)"


def _why_not_scored(reason: str) -> str:
    """The short reason a window was not compared as scored: `diff_scored`'s
    message without its frame ("the older snapshot could not be mapped")."""
    return reason.removeprefix(_NOT_SCORED).removesuffix(_RAW_ONLY)


def diff_scored(
    older: dict,
    newer: dict,
    *,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    since: date | None = None,
) -> ScoredDiff:
    """Every scored, non-split field compared as `build_dataset` builds it,
    on the reported quarter ends both snapshots cover.

    The mapper is the one resolver of what the engine scores. The raw fact
    diff followed one tag per field chosen by `_active_tag` — an
    approximation that can pick a tag the mapper does not score — and saw
    nothing built from several concepts (total debt, a composite, or D&A
    standing on depreciation alone). Raw facts are now PROVENANCE: a change
    to a single-concept figure carries the filing of the fact behind it,
    and a raw revision of a scored tag for a period older than the reported
    window is shown as context (never promoted). Any other raw row is not
    about a scored figure and is dropped.

    A quarter whose value was built differently in the two snapshots
    (another strategy or other components) is a change of composition:
    reported with the new composition, never promoted.

    When either snapshot cannot be mapped the raw fact rows are all there
    is, and they are returned with `scope="raw"`: still listed, never a
    scored change. Left `"scored"`, a partial fetch's missing concepts read
    as scored withdrawals and a raw tag the engine does not score reached
    Tier 1 and the ledger's VALIDATED (Hermes audit of 424b0b4, finding 2).
    """
    raw = diff_vintages(older, newer, materiality_pct=materiality_pct, since=since)
    a, b = _mapped(older), _mapped(newer)
    if a is None or b is None:
        which = "older" if a is None else "newer"
        return ScoredDiff(
            [replace(c, scope="raw") for c in raw],
            f"{_NOT_SCORED}the {which} snapshot could not be mapped{_RAW_ONLY}",
        )
    window_start = min(b.window, default=None)

    changes: list[VintageChange] = []
    for spec in FIELDS:
        name = spec.name
        if name in SPLIT_ADJUSTED_FIELDS:
            continue
        unit = _unit_for(name)
        old_series, new_series = a.values.get(name, {}), b.values.get(name, {})
        for end, old in sorted(old_series.items()):
            if since is not None and end < since:
                continue
            old_src = a.sources.get(name, {}).get(end, ("", ()))
            new_src = b.sources.get(name, {}).get(end, old_src)
            new = new_series.get(end)
            if new is None:
                # Withdrawn only if the newer snapshot still reports that
                # quarter; one that rolled out of its window was not.
                if window_start is not None and end >= window_start:
                    key = FactKey(COMPOSED, _bare(old_src[1]), unit, None, end)
                    changes.append(VintageChange("withdrawn", name, key, old, None, "", ""))
                continue
            if new == old:
                continue
            pct = None if old == 0 else abs(new - old) / abs(old)
            if pct is not None and pct < materiality_pct:
                continue
            recomposed = new_src != old_src
            moved_to = _bare(new_src[1]) if recomposed else ""
            single = old_src[0] == "single" and len(old_src[1]) == 1 and not recomposed
            before = _filing(older, old_src[1][0], unit, end) if single else None
            after = _filing(newer, old_src[1][0], unit, end) if single else None
            if before is not None and after is not None:
                taxonomy, _sep, tag = old_src[1][0].partition(":")
                key = FactKey(taxonomy, tag, unit, before[3], end)
                changes.append(VintageChange(
                    "revised", name, key, old,
                    before[0], before[1], before[2], new, after[0], after[1], after[2], pct,
                    original_retained=(after[1] != before[1]
                                       and _retains(older, newer, key, before[1])),
                ))
            else:
                label = _bare(old_src[1]) + (" (partial)" if old_src[0] == "partial" else "")
                changes.append(VintageChange(
                    "revised", name, FactKey(COMPOSED, label, unit, None, end), old,
                    None, "", "", new, None, "", "", pct, moved_to,
                ))

    # Raw revisions of a scored tag older than the reported window: context.
    scored_tags = {
        (name, c.split(":", 1)[-1])
        for m in (a, b) for name, by_q in m.sources.items()
        for _strategy, comps in by_q.values() for c in comps
    }
    for c in raw:
        if window_start is not None and c.key.end < window_start \
                and (c.field_name, c.key.tag) in scored_tags:
            changes.append(replace(c, scope="context"))
    changes.sort(key=lambda c: (c.key.end, c.field_name, c.key.tag), reverse=True)
    return ScoredDiff(changes)


def _filing(facts: dict, component: str, unit: str, end: date) -> tuple | None:
    """(filed, accession, form, start) of the fact a single-concept value was
    read from: the concept's current fact ending on `end`, the shortest
    period first — a quarter's own fact before the year-to-date fact it may
    have been derived from — then the shared `precedence` order the mapper
    reads values by (latest filed, an amendment over an original on the
    same day, the higher accession). Read from the snapshot itself, so
    provenance never depends on which tag the raw diff happened to follow."""
    taxonomy, _sep, tag = component.partition(":")
    best: tuple | None = None
    for row in _rows(facts, taxonomy, tag, unit):
        try:
            if _parse_date(row["end"]) != end:
                continue
            filed = _parse_date(row["filed"])
            start = _parse_date(row["start"]) if row.get("start") else None
        except (KeyError, TypeError, ValueError):
            continue
        form, accn = row.get("form", ""), row.get("accn", "")
        order = (start or end, rank(filed, form, accn))
        if best is None or order > best[0]:
            best = (order, (filed, accn, form, start))
    return None if best is None else best[1]


def _change_row(c: VintageChange) -> tuple[str, str, str]:
    """(now, change, originally filed) cells of one change's row."""
    composed = c.key.taxonomy == COMPOSED
    now = "withdrawn" if c.kind == "withdrawn" else f"{c.new_value:,.0f}"
    if c.scope == "context":
        now += " (before the scored window)"
    elif c.scope == "raw":
        # The raw fallback reads only the tags the engine scores a field
        # from, so the row IS about a scored field — just not compared as
        # the mapper builds it (review of 224b896, finding 2).
        now += " (raw fact of a scored field's tag; not compared as the engine builds it)"
    if c.moved_tag:
        now += f" (now {'built from' if composed else 'tagged'} {c.new_tag})"
    pct = f"{c.pct_change:.1%}" if c.pct_change is not None else "—"
    if composed:
        # No single filing carries this figure: say what it was built from.
        filed = f"built from {c.key.tag}"
    else:
        filed = f"{c.old_filed} {c.old_form} {c.old_accession}".strip()
    return now, pct, filed


def render_changes(
    changes: list[VintageChange], older: str, newer: str, *, unavailable: str | None = None
) -> str:
    """One markdown section. Says plainly when nothing moved — an empty diff
    is the expected result most of the time and is worth stating.

    `unavailable`: why this window was not compared as scored. It is said
    first, and no "nothing changed" sentence follows: an empty or raw-only
    diff there is a comparison never made, not a clean one (review of
    c131583, finding 2). Any raw rows are listed after it.

    A figure that moved with a later filing the newer snapshot carries beside
    the original (`explained_by_filing`) is listed apart, never under the
    "no amended filing behind it" framing: the within-snapshot detector reads
    it from filing history, and an amendment is the ordinary case."""
    head = f"### Vintage diff — {older} → {newer}\n"
    lead = [f"Not compared as scored: {_why_not_scored(unavailable)}."] if unavailable else []
    if not changes:
        if lead:
            return head + "\n" + lead[0] + "\n"
        return head + "\nNo prior-period figure changed or disappeared between these snapshots.\n"
    silent = [c for c in changes if not c.explained_by_filing]
    filed = [c for c in changes if c.explained_by_filing]
    lines = [head, lead[0], ""] if lead else [head, ""]
    if silent:
        lines += [
            "_Context, not an alarm._ Nothing found here has an amended filing "
            "behind it — that is what makes it invisible to the within-snapshot "
            "detector, and it also means the ordinary explanations come first: a "
            "discontinued operation or a spinoff re-presented, a segment "
            "reclassification, a taxonomy migration. Read the filing before "
            "calling any of it a restatement.",
            "",
            "| Field | Period | Was | Now | Change | Originally filed |",
            "|---|---|---|---|---|---|"]
        for c in silent:
            now, pct, orig = _change_row(c)
            lines.append(
                f"| {c.field_name} | {c.key.period} | {c.old_value:,.0f} | {now} | {pct} | {orig} |")
    elif not lead:
        lines.append("No prior-period figure changed silently between these snapshots.")
    if filed:
        lines += [
            "",
            "**Moved with a later filing (not silent).** The newer snapshot carries "
            "the original fact beside the filing that revised it, so the restatement "
            "scan reads these from filing history; listed so the snapshot trail is "
            "complete.",
            "",
            "| Field | Period | Was | Now | Change | Originally filed | Revised by |",
            "|---|---|---|---|---|---|---|"]
        for c in filed:
            now, pct, orig = _change_row(c)
            by = f"{c.new_filed} {c.new_form} {c.new_accession}".strip()
            lines.append(
                f"| {c.field_name} | {c.key.period} | {c.old_value:,.0f} | {now} | {pct} "
                f"| {orig} | {by} |")
    return "\n".join(lines) + "\n"


# Tier-1 promotion threshold for a silent revision: hand-set and UNCALIBRATED.
# Five times the diff's own 1% materiality floor. The report section lists
# every >=1% move as context; the decision card promotes only the ones large
# enough that "a reclassification" is not the obvious first explanation.
# P1-F kill criterion applies from 2026-09-22: two quarters of live coverage
# producing only noise diffs demotes the Tier-1 line to appendix-only.
SILENT_REVISION_TIER1_PCT = 0.05


def observation_at_or_before(
    states: list[VintageObservation], day: date
) -> VintageObservation | None:
    """Newest observation captured on or before `day`, or None.

    `states` is `observed_vintages` order (oldest first, same-day order as
    the manifest recorded it), so the last hit is the answer. Not sorted
    here: two same-day states sort by hash if sorted by name, and the
    manifest's order is the only record of which came second."""
    hit: VintageObservation | None = None
    for state in states:
        if date.fromisoformat(state.captured) <= day:
            hit = state
    return hit


@dataclass(frozen=True)
class VintageDiffReport:
    """What the silent-revision check compared for one report, and found.

    Two senses of "baseline" meet here, so both are named. `baseline` is the
    observation at or before the pinned thesis day — "what moved since you
    locked". `no_baseline_reason` is the older sense from the data-quality
    line: there was nothing earlier to diff the newest snapshot against, so
    NOTHING was compared and the section must say so rather than read as
    clean.

    `changes_since_baseline` is None when no thesis day was given or when the
    thesis-day observation is the previous or the newest one (a second
    comparison would repeat the first, or compare a snapshot with itself);
    `baseline_note` then says which.
    """

    as_of: date
    newest: VintageObservation | None
    previous: VintageObservation | None
    changes_since_previous: list[VintageChange]
    baseline: VintageObservation | None = None
    changes_since_baseline: list[VintageChange] | None = None
    no_baseline_reason: str | None = None
    baseline_note: str | None = None
    # Why scored values were not compared as the engine builds them (a
    # snapshot the mapper cannot build), when they were not: the previous ->
    # newest window, and the thesis-lock window apart. One window compared
    # as scored says nothing about the other (review of 224b896, finding 2).
    canonical_unavailable: str | None = None
    baseline_unavailable: str | None = None
    # Raw captures passed over for the baseline (`report_diff`), when any were.
    raw_note: str | None = None
    # The report named its payload and the store does not hold it: nothing
    # was compared, and "no baseline yet" would be the wrong reason.
    payload_missing: bool = False
    # What the thesis-lock baseline is when it is not a state a report
    # scored: `SWEEP_BASELINE` (review of c131583, finding 1), or
    # `UNRECORDED_BASELINE` (cross-branch review of e0525c4).
    baseline_source: str | None = None
    # Why no state before the thesis day could stand as the lock baseline,
    # short, for the card and the ledger (with `baseline_unavailable` and
    # no `baseline`).
    lock_gap: str | None = None

    @property
    def compared(self) -> bool:
        return self.no_baseline_reason is None

    @property
    def tier1_gap(self) -> str | None:
        """Why the decision card must list silent revisions as not checked
        this run, or None when every window was compared as the engine
        scores it. A window that fell back to raw fact rows promotes nothing
        (`scope="raw"`), so without this the card read clean over a
        comparison never made (review of 224b896, finding 2)."""
        if not self.compared:
            if self.payload_missing:
                return "the report's payload is not in the vintage store"
            return "no vintage baseline yet"
        # Which window, when only one was not compared as scored: "not
        # compared as scored" alone left the reader to guess whether the
        # other was checked (review of c131583, nit).
        if self.canonical_unavailable and self.baseline_unavailable:
            return "not compared as scored in either window: a snapshot could not be mapped"
        if self.canonical_unavailable:
            return "since the previous report: not compared as scored, a snapshot could not be mapped"
        if self.baseline_unavailable:
            if self.baseline is None:
                # No state before the thesis day could stand as the baseline
                # (`report_diff`): none maps, or the captures that do are
                # incomplete (review of 626ca1b, finding 1).
                return ("since the pinned thesis: not compared as scored, "
                        f"{self.lock_gap or 'no snapshot before the thesis day could be mapped'}")
            return "since the pinned thesis: not compared as scored, a snapshot could not be mapped"
        return None

    def status_line(self) -> str:
        """One line for the data-quality section: exactly what was compared."""
        if not self.compared:
            return self.no_baseline_reason or "not compared"
        assert self.newest is not None and self.previous is not None
        # A window not compared as scored says so where it is named, with
        # its reason: "compared X → Y: not compared as scored" contradicted
        # itself (review of c131583, nit).
        window = f"{self.previous.captured} → {self.newest.captured}"
        line = (
            f"{window} {_count(self.changes_since_previous, self.canonical_unavailable)}"
            if self.canonical_unavailable
            else f"compared {window}: {_count(self.changes_since_previous)}"
        )
        if self.changes_since_baseline is not None and self.baseline is not None:
            lock = f"since pinned thesis {self.baseline.captured}"
            if self.baseline_source:
                lock += f", {self.baseline_source}"
            line += (
                f"; {lock} {_count(self.changes_since_baseline, self.baseline_unavailable)}"
                if self.baseline_unavailable
                else f"; {lock}: {_count(self.changes_since_baseline)}"
            )
        elif self.baseline_unavailable:
            line += f"; since the pinned thesis not compared as scored: {self.baseline_unavailable}"
        if self.baseline_note:
            line += f"; {self.baseline_note}"
        if self.raw_note:
            line += f"; {self.raw_note}"
        return line


def _count(changes: list[VintageChange], unavailable: str | None = None) -> str:
    """"N change(s)" counts the silent ones; moves a later filing explains
    are counted apart, so an amendment never reads as a silent change, and
    so are raw fact rows (a snapshot that could not be mapped), which are not
    changes to a scored figure at all, and figures now built from other
    concepts (`moved_tag`: a change of composition, which Tier 1 and the
    card's revised-input notes skip too — counted as "changes" it read as
    silent revisions; review of 626ca1b, finding 1). A window that was not
    compared as scored (`unavailable`) says so, with the reason, instead of
    "0 change(s)": its raw rows are never promoted, so a zero there was a
    count of nothing checked."""
    raw = sum(c.scope == "raw" for c in changes)
    filed = sum(c.explained_by_filing for c in changes if c.scope != "raw")
    moved = sum(c.moved_tag and not c.explained_by_filing for c in changes if c.scope != "raw")
    text = (
        f"not compared as scored: {_why_not_scored(unavailable)}" if unavailable
        else f"{len(changes) - raw - filed - moved} change(s)"
    )
    text += f" (+{filed} moved with a later filing, not silent)" if filed else ""
    text += (f" (+{moved} built from other concepts: a change of composition, not a revision)"
             if moved else "")
    return text + (f" (+{raw} raw fact row(s), not scored changes)" if raw else "")


def _load_for_diff(obs: VintageObservation) -> dict:
    """A stored snapshot for the report's diff. An unreadable file is a data
    failure — the stored SEC payload cannot be read — and is reported as one
    (`ExternalPayloadError`), never mistaken for a defect in this code."""
    try:
        return load_vintage(obs.path)
    except UNREADABLE as e:
        raise ExternalPayloadError(
            f"vintage snapshot {obs.path.name} unreadable: {type(e).__name__}: {e}"
        ) from e


def _newest_scored(
    states: list[VintageObservation], is_scored: Callable[[VintageObservation], bool]
) -> int | None:
    """Index of the newest state in `states` (observation order) that
    `is_scored` accepts. An index, not the state: one content observed twice
    on one day is two equal observations."""
    for i in range(len(states) - 1, -1, -1):
        if is_scored(states[i]):
            return i
    return None


def _passed_over(obs: VintageObservation, e: BaseException) -> None:
    """Log a stored state passed over while choosing one: the status line
    counts them, the log says which and why (review of c131583, finding 3)."""
    logger.warning("vintage snapshot %s (captured %s, kind %s) passed over: %s: %s",
                   obs.path.name, obs.captured, obs.kind or "unrecorded", type(e).__name__, e)


# The thesis-lock walk maps at most this many captures before the thesis
# day (a capture whose content was already built costs nothing and is still
# examined); older ones are counted as not examined. A covering capture is
# normally the newest, and each costs up to two mapper builds (review of
# 626ca1b, finding 4). Hand-set.
LOCK_CAPTURES_EXAMINED = 8
# `baseline_source` of a lock baseline that is a watch-sweep capture.
SWEEP_BASELINE = "a sweep capture (mapped, complete)"
# ... and of one whose kind the manifest does not record (cross-branch
# review of e0525c4): held to the same coverage rule as a capture.
UNRECORDED_BASELINE = "a stored snapshot of unrecorded kind (mapped, complete)"


def _passed_over_for_lock(obs: VintageObservation, why: str,
                          baseline: str = "the thesis-lock baseline") -> None:
    """Log a capture the thesis-lock walk passed over: the status line counts
    them, the log says which and why (review of 626ca1b, finding 5). A state
    of unrecorded kind the previous-report walk passes over is logged the
    same way, naming that baseline."""
    logger.warning("vintage snapshot %s (captured %s, kind %s) passed over for %s: %s",
                   obs.path.name, obs.captured, obs.kind or "unrecorded", baseline, why)


def report_diff(
    cik: int,
    *,
    as_of: date,
    baseline_day: date | None = None,
    since: date | None = None,
    root: Path | None = None,
    scored_sha: str | None = None,
) -> VintageDiffReport:
    """The silent-revision check for a report dated `as_of`.

    Only observations captured on or before `as_of` are visible — the one
    place the report's date bounds this store, so a historical replay passes
    a past date and gets the trail as it stood. The newest visible SCORED
    state is diffed against the newest earlier scored one of different
    content; with a `baseline_day` (the pinned thesis day on the journal
    track) the newest is also diffed against the last scored observation
    captured BEFORE that day, unless that is already one of the two.
    Strictly before: snapshots are dated by day, so one captured on the lock
    day may postdate the lock, and a revision it carried would be absorbed
    into the baseline and never reported. Erring the other way re-reports at
    most one day of pre-lock changes. An unreadable snapshot raises
    `ExternalPayloadError`: that is a data failure for the caller's stream
    containment, not a "no baseline" state.

    Scored states only (Hermes audit of 424b0b4, finding 2). A raw capture
    — the watch sweep's fetch, never mapped, possibly partial — replaced the
    state a report scored: taken as the baseline, an unmappable one turned
    the comparison into raw fact rows promoted to Tier 1 though nothing
    scored had moved, and a bare one hid a real revision (additions are
    never reported); taken as the newest, a partial fetch read as scored
    withdrawals. Raw captures passed over are counted in the status line
    and stay in the store for `scripts/vintage.py diff`.

    `scored_sha` is the digest of the payload the report scored. That
    content is scored for this comparison whatever wrote it, and it IS the
    newest state compared: the report's own `store_snapshot` may not have
    landed (busy, failed, `--no-vintage`) after the sweep captured the same
    content as raw, and a replay scores whichever stored state it chose.
    Left to "the newest scored state", the first compared an older pair and
    promoted a stale line; the second dropped the replayed payload as raw
    (review of 224b896, finding 1). When the store does not hold that
    payload, nothing is compared and the reason says so — never some other
    pair. Without it (a caller that holds no payload) the newest scored
    state stands in, as before.

    The thesis-lock baseline is the NEAREST state before the thesis day
    that is either scored or a watch-sweep capture that covers the scored
    comparison (`_coverage_gaps`: every field and quarter the scored state
    nearest after the thesis day held when the capture was fetched, built
    the same way), a capture labelled `baseline_source` (review of c131583,
    finding 1). A capture that merely maps was taken before: one missing a
    field or the latest quarters hid a real revision and read clean, and
    one missing a component read as recomposed "changes"; and an old scored
    state beat a covering capture from the day before the lock, so a
    revision public before the lock read as one since (review of 626ca1b,
    findings 1 and 3). Captures passed over are logged and counted; each
    content is mapped once per report, and at most
    `LOCK_CAPTURES_EXAMINED` captures are mapped (finding 4). When nothing
    before the day qualifies, `baseline_unavailable` says the lock window
    was not compared as scored.

    A state whose kind the manifest does not record (written before kinds
    existed, or rebuilt from disk after the manifest was lost) stands in for
    a scored one as a baseline — the previous report's or the thesis lock's
    — only if it covers the comparison, like a raw capture: taken whenever
    it mapped, a partial one hid a real revision ("since pinned thesis ...:
    0 change(s)"). Only the payload this report scored and a state recorded
    as scored are a baseline, or the lock's reference, as they are; the
    status line says how many stored states are of unrecorded kind
    (cross-branch review of e0525c4).
    """
    visible = [
        s for s in observed_vintages(cik, root) if date.fromisoformat(s.captured) <= as_of
    ]
    if not visible:
        return VintageDiffReport(
            as_of, None, None, [],
            no_baseline_reason=(
                f"no snapshot at or before {as_of} (capture disabled or failed — "
                "see the Vintage snapshot line)"
            ),
        )
    top: int | None = None
    if scored_sha is not None:
        top = next((i for i in range(len(visible) - 1, -1, -1)
                    if visible[i].sha256 == scored_sha), None)
        if top is None:
            return VintageDiffReport(
                as_of, None, None, [],
                no_baseline_reason=(
                    f"the payload this report scored is not in the vintage store at or "
                    f"before {as_of} (see the Vintage snapshot line); not compared"
                ),
                payload_missing=True,
            )
    if len(visible) == 1:
        return VintageDiffReport(
            as_of, visible[-1], None, [],
            no_baseline_reason=(
                f"only one snapshot observed at or before {as_of}; nothing earlier "
                "to diff against yet"
            ),
        )
    # Each content is built once per report, whichever walk asks — the
    # legacy classification or the thesis-lock walk (review of 626ca1b,
    # finding 4); one the mapper trips on is kept as its exception.
    built: dict[str, _Mapped | BaseException | None] = {}
    unusable: set[str] = set()

    def content(obs: VintageObservation) -> str:
        return obs.sha256 or str(obs.path)

    def build(obs: VintageObservation) -> _Mapped | BaseException | None:
        key = content(obs)
        if key not in built:
            try:
                built[key] = _mapped(_load_for_diff(obs))
            except UNUSABLE as e:
                built[key] = e
        return built[key]

    def trusted(obs: VintageObservation) -> bool:
        # Known to be scored: the payload this report scored, or a state the
        # manifest records a report scored. Only these stand as a baseline
        # as they are; a state of unrecorded kind that maps may be a partial
        # sweep capture, and must cover the comparison like a raw one
        # (cross-branch review of e0525c4).
        return (scored_sha is not None and obs.sha256 == scored_sha) or obs.kind == SCORED

    def is_scored(obs: VintageObservation) -> bool:
        # A store written before kinds existed (or one whose manifest was
        # rebuilt from disk) does not say what wrote a state, so decide by
        # whether the mapper builds it — the test `diff_scored` applies
        # anyway. Treating unknown as raw would leave every existing store
        # without a baseline until two new reports had been scored; the walk
        # stops at the first mappable state, normally the first looked at,
        # so this costs about one extra build per report. A legacy partial
        # capture that still maps cannot be told apart: as the newest state
        # it is used, as before; as a baseline it must also cover the
        # comparison (`coverage_gaps`) — taken as it was, one missing Assets
        # hid a real +20% revision (cross-branch review of e0525c4).
        # One that cannot be read, or whose odd shape the mapper trips on,
        # is not a usable state: passed over and counted, not a stream
        # failure over a snapshot no comparison needed (review of 224b896,
        # finding 4).
        if trusted(obs):
            return True
        if obs.kind is not None:
            return False
        got = build(obs)
        if isinstance(got, BaseException):
            # Legacy only: SCORED and RAW returned above. Nothing says a
            # report scored it, so it is passed over — and said so, once
            # (review of c131583, finding 3).
            if content(obs) not in unusable:
                _passed_over(obs, got)
                unusable.add(content(obs))
            return False
        return got is not None

    notes: list[str] = []
    # Stored snapshots of unrecorded kind, by file: every mappable one stood
    # in for a scored state before, so the reader is told the kinds are
    # unknown, not only which were passed over.
    unrecorded = len({s.path for s in visible if s.kind is None})

    def raw_note() -> str | None:
        extra = (
            [f"{len(unusable)} snapshot(s) passed over were unreadable or malformed"]
            if unusable else []
        )
        if unrecorded:
            extra.append(f"{unrecorded} stored snapshot(s) of unrecorded kind (manifest "
                         "rebuilt or written before kinds)")
        return "; ".join([*notes, *extra]) or None

    loaded: dict[str, dict] = {}

    def facts_of(obs: VintageObservation) -> dict:
        if content(obs) not in loaded:
            loaded[content(obs)] = _load_for_diff(obs)
        return loaded[content(obs)]

    # A mapped state stands in for a scored one only if it covers the
    # comparison: checked against a reference state (the scored side it
    # will be compared with, or the one nearest after the thesis day) as it
    # stood when the state was fetched — a quarter filed later is not a
    # gap. The fetch time is known only to the day, and on a filing day it
    # may precede or follow that day's filing (one after it can also move a
    # selection), so the state must match the reference as of the day
    # before or as of the day itself.
    ref_as_of: dict[tuple[str, date], _Mapped | BaseException | None] = {}

    def gaps_as_of(capture_map: _Mapped, ref_obs: VintageObservation, day: date) -> list[str]:
        key = (content(ref_obs), day)
        if key not in ref_as_of:
            try:
                ref_as_of[key] = _mapped(facts_of(ref_obs), as_of=day)
            except UNUSABLE as e:
                ref_as_of[key] = e
        ref = ref_as_of[key]
        if isinstance(ref, _Mapped):
            return _coverage_gaps(capture_map, ref, since)
        return ["it cannot be built as of that day"
                + (f" ({type(ref).__name__}: {ref})" if ref is not None else "")]

    def coverage_gaps(obs: VintageObservation, capture_map: _Mapped,
                      ref_obs: VintageObservation) -> list[str]:
        """The gaps as of the day before `obs` was fetched, or none when it
        covers as of either day."""
        day = date.fromisoformat(obs.captured)
        gaps = gaps_as_of(capture_map, ref_obs, day - timedelta(days=1))
        return gaps if gaps and gaps_as_of(capture_map, ref_obs, day) else []

    if top is None:
        top = _newest_scored(visible, is_scored)
        if top is None:
            reason = (f"no scored snapshot at or before {as_of}; {len(visible)} raw "
                      "capture(s) not used")
            return VintageDiffReport(as_of, visible[-1], None, [],
                                     no_baseline_reason="; ".join(filter(None, [reason, raw_note()])))
        if top < len(visible) - 1:
            notes.append(f"{len(visible) - 1 - top} raw capture(s) after "
                         f"{visible[top].captured} not compared")
    elif top < len(visible) - 1:
        notes.append(f"{len(visible) - 1 - top} later snapshot(s) after {visible[top].captured} "
                     "not compared (not the payload this report scored)")
    newest = visible[top]
    # Not the newest's own content: a state that went S -> raw -> S has not
    # changed as scored, and comparing it with itself would say "0 changes"
    # about a comparison never made.
    earlier = [s for s in visible[:top] if not (s.sha256 and s.sha256 == newest.sha256)]
    # Nearest first: a state known to be scored is the baseline as it is; one
    # of unrecorded kind that maps only if it covers the newest.
    at: int | None = None
    passed = 0  # of unrecorded kind: mapped, but incomplete against the newest
    for i in range(len(earlier) - 1, -1, -1):
        if trusted(earlier[i]):
            at = i
            break
        if not is_scored(earlier[i]):
            continue  # raw, or a legacy state that does not map
        got = build(earlier[i])
        assert isinstance(got, _Mapped)  # `is_scored` built it
        gaps = coverage_gaps(earlier[i], got, newest)
        if not gaps:
            at = i
            break
        _passed_over_for_lock(earlier[i], f"incomplete against the newest ({newest.captured}): "
                                          f"{'; '.join(gaps)}", "the previous-report baseline")
        passed += 1
    if at is None:
        reason = f"no earlier scored snapshot to diff the newest ({newest.captured}) against"
        if len(earlier) > passed:
            notes.insert(0, f"{len(earlier) - passed} raw capture(s) not used as the baseline")
        if passed:
            notes.insert(0, f"{passed} snapshot(s) of unrecorded kind not used as the baseline "
                            "(incomplete)")
        return VintageDiffReport(as_of, newest, None, [],
                                 no_baseline_reason="; ".join(filter(None, [reason, raw_note()])))
    previous = earlier[at]
    if at < len(earlier) - 1 - passed:
        notes.insert(0, f"{len(earlier) - 1 - at - passed} raw capture(s) since "
                        f"{previous.captured} not used as the baseline")
    if passed:
        notes.insert(0, f"{passed} snapshot(s) of unrecorded kind since {previous.captured} "
                        "not used as the baseline (incomplete)")
    new_facts = facts_of(newest)
    scored = diff_scored(facts_of(previous), new_facts, since=since)
    changes, unavailable = scored.changes, scored.canonical_unavailable
    if baseline_day is None:
        return VintageDiffReport(as_of, newest, previous, changes,
                                 canonical_unavailable=unavailable, raw_note=raw_note())

    lock_day = baseline_day - timedelta(days=1)
    before_lock = [s for s in visible if date.fromisoformat(s.captured) <= lock_day]
    if not before_lock:
        # Nothing at all was stored before the thesis day: nothing can be
        # back-filled, so this is a note, not a window left unchecked.
        return VintageDiffReport(
            as_of, newest, previous, changes,
            baseline_note=(
                f"no snapshot before the pinned thesis day {baseline_day}; "
                f"earliest is {visible[0].captured}"
            ),
            canonical_unavailable=unavailable, raw_note=raw_note(),
        )
    # A capture is checked against the scored state nearest after the
    # thesis day (the newest when none is nearer): the least time for the
    # filer to have legitimately changed how a figure is built. A state of
    # unrecorded kind is not a reference: a partial one would let a capture
    # missing the same field cover it (cross-branch review of e0525c4).
    reference = visible[next((i for i in range(top + 1) if date.fromisoformat(visible[i].captured)
                              > lock_day and trusted(visible[i])), top)]

    baseline: VintageObservation | None = None
    source = None
    unmappable = incomplete = mapped_here = untried = 0
    legacy = raw = 0  # which kinds were passed over, for the wording
    # Nearest first: a state known to be scored qualifies as it is; a raw
    # capture, or a state of unrecorded kind, only if it maps and covers the
    # comparison.
    for obs in reversed(before_lock):
        if trusted(obs):
            baseline = obs
            break
        day = date.fromisoformat(obs.captured)
        got_before = built.get(content(obs))
        if obs.kind is None and content(obs) in unusable:
            continue  # the previous-report walk passed it over: logged and counted
        costly = content(obs) not in built or (
            isinstance(got_before, _Mapped)
            and (content(reference), day - timedelta(days=1)) not in ref_as_of)
        if costly and mapped_here >= LOCK_CAPTURES_EXAMINED:
            untried += 1
            continue
        mapped_here += costly
        got = build(obs)
        if not isinstance(got, _Mapped):
            _passed_over_for_lock(obs, f"{type(got).__name__}: {got}" if got is not None
                                  else "does not map (the mapper cannot build it)")
            unmappable += 1
        elif gaps := coverage_gaps(obs, got, reference):
            _passed_over_for_lock(obs, f"incomplete against the scored state of "
                                       f"{reference.captured} as of {day - timedelta(days=1)}: "
                                       f"{'; '.join(gaps)}")
            incomplete += 1
        else:
            baseline, source = obs, SWEEP_BASELINE if obs.kind == RAW else UNRECORDED_BASELINE
            break
        legacy += obs.kind is None
        raw += obs.kind == RAW
    what = ("sweep capture(s)" if not legacy
            else "stored snapshot(s) of unrecorded kind" if not raw
            else "sweep capture(s) or stored snapshot(s) of unrecorded kind")
    if unmappable or incomplete:
        kinds = [f"{unmappable} unmappable" if unmappable else "",
                 f"{incomplete} incomplete" if incomplete else ""]
        noun = ("capture(s)" if not legacy else "snapshot(s) of unrecorded kind" if not raw
                else "capture(s) or snapshot(s) of unrecorded kind")
        notes.append(f"{unmappable + incomplete} {noun} before the thesis day not usable as "
                     f"the lock baseline ({', '.join(k for k in kinds if k)})")
    if untried:
        notes.append(f"{untried} older capture(s) before the thesis day not examined (at most "
                     f"{LOCK_CAPTURES_EXAMINED} per report)")
    if baseline is None:
        # States predate the thesis day and none qualifies: the lock window
        # is not compared as scored, and the card says so.
        if untried:
            why = (f"no snapshot before the pinned thesis day {baseline_day} was usable among "
                   f"the {unmappable + incomplete} examined")
            gap: str | None = "no snapshot examined before the thesis day could be used"
        elif incomplete:
            gap = (f"incomplete {what} only" if not unmappable
                   else f"incomplete or unmappable {what} only")
            why = f"no snapshot before the pinned thesis day {baseline_day} covers the scored comparison: {gap}"
        else:
            why, gap = f"no snapshot before the pinned thesis day {baseline_day} can be mapped", None
        scored_by = ("none recorded as scored by a report"
                     if any(s.kind is None for s in before_lock) else "none scored by a report")
        return VintageDiffReport(
            as_of, newest, previous, changes,
            canonical_unavailable=unavailable, raw_note=raw_note(),
            baseline_unavailable=f"{why} ({len(before_lock)} stored before it, {scored_by})",
            lock_gap=gap,
        )
    # By content, not identity: a revert (A -> B -> A) is a third observation
    # that reuses A's bytes, and diffing it against the newest A finds nothing.
    if baseline.sha256 == newest.sha256:
        note = (
            f"the pinned thesis snapshot ({baseline.captured}) is the newest "
            "snapshot; nothing to compare since the lock"
        )
        return VintageDiffReport(as_of, newest, previous, changes, baseline,
                                 baseline_note=note, canonical_unavailable=unavailable,
                                 raw_note=raw_note(), baseline_source=source)
    if baseline.sha256 == previous.sha256:
        note = (
            f"the pinned thesis snapshot ({baseline.captured}) is the previous "
            "snapshot; one comparison covers both"
        )
        return VintageDiffReport(as_of, newest, previous, changes, baseline,
                                 baseline_note=note, canonical_unavailable=unavailable,
                                 raw_note=raw_note(), baseline_source=source)
    lock = diff_scored(facts_of(baseline), new_facts, since=since)
    return VintageDiffReport(as_of, newest, previous, changes, baseline, lock.changes,
                             canonical_unavailable=unavailable,
                             baseline_unavailable=lock.canonical_unavailable,
                             raw_note=raw_note(), baseline_source=source)


def tier1_promotions(
    rep: VintageDiffReport, *, period_since: date
) -> list[tuple[VintageChange, VintageObservation, VintageObservation, str]]:
    """The decision card's silent-revision lines, each fact — (field,
    period start, period end) — once: `(change, older, newer, line)`, with
    `older -> newer` the window whose `change` put it on the card.

    Promote from BOTH windows, each fact once. The lock-to-now window
    catches a revision that landed in an intermediate state (invisible to
    previous -> newest); previous -> newest catches a revision to a period
    the lock snapshot did not yet contain (a quarter added after the lock,
    then quietly revised), which the lock-to-now diff cannot see because it
    only walks facts present in the older snapshot.

    A fact is "promoted" once a window actually put it on the card. Every
    key the lock window listed used to count — a raw row (never promoted)
    or a move below the threshold since the lock suppressed a real Tier-1
    revision in previous -> newest while the ledger marked it VALIDATED
    (review of 224b896, finding 3). The ledger reads this same list, so a
    fact both windows promote is one card line and one VALIDATED item
    (review of c131583, finding 4)."""
    newest, previous, baseline = rep.newest, rep.previous, rep.baseline
    # `compared` (and a lock window) imply both snapshots exist; the explicit
    # None checks only let the type checker see it.
    windows: list[tuple[list[VintageChange], VintageObservation, VintageObservation]] = []
    if rep.changes_since_baseline is not None and baseline is not None and newest is not None:
        windows.append((rep.changes_since_baseline, baseline, newest))
    if rep.compared and previous is not None and newest is not None:
        windows.append((rep.changes_since_previous, previous, newest))
    out: list[tuple[VintageChange, VintageObservation, VintageObservation, str]] = []
    promoted: set[tuple] = set()
    for changes, older, newer in windows:
        for c in changes:
            key = (c.field_name, c.key.start, c.key.end)
            if key in promoted:
                continue
            lines = silent_revision_tier1_lines(
                [c], older.captured, newer.captured, period_since=period_since)
            if lines:
                out += [(c, older, newer, line) for line in lines]
                promoted.add(key)
    return out


def silent_revision_tier1_lines(
    changes: list[VintageChange], older: str, newer: str, *, period_since: date
) -> list[str]:
    """Tier-1 lines for the decision card, one per promoted change.

    Promoted: a `revised` scored, non-split field, for a period ending on or
    after `period_since`, moved by at least SILENT_REVISION_TIER1_PCT, not a
    tag migration, and not explained by a later filing the newer snapshot
    carries beside the original (the restatement scan's line covers that one:
    promoting it too put an amendment on the card twice, once as "silent"). A figure revised away from zero (an impairment of 0
    restated to 500M) has no percentage and is promoted too: it cannot be
    immaterial. Withdrawn facts and tag moves stay in the appendix section: a
    withdrawal has no ratio to threshold, and a move is a filer re-tagging
    the same number until proven otherwise. Order is the diff's (period
    descending)."""
    # Every field the engine scores — including total debt, which no
    # single-tag table lists and whose changes come from `diff_scored`.
    scored = {f.name for f in FIELDS}
    out: list[str] = []
    for c in changes:
        if c.kind != "revised" or c.new_value is None or c.scope != "scored":
            continue
        if c.field_name not in scored or c.field_name in SPLIT_ADJUSTED_FIELDS:
            continue
        if c.key.end < period_since:
            continue
        from_zero = c.old_value == 0 and c.new_value != 0
        if not from_zero and (c.pct_change is None or c.pct_change < SILENT_REVISION_TIER1_PCT):
            continue
        if c.moved_tag or c.explained_by_filing:
            continue
        move = (
            "from zero" if from_zero
            else f"{(c.new_value - c.old_value) / abs(c.old_value):+.1%}"
        )
        out.append(
            f"Silent revision: {c.field_name} for {c.key.period} "
            f"{c.old_value:,.0f} → {c.new_value:,.0f} ({move}) between snapshots "
            f"{older} and {newer} (detail in appendix; threshold hand-set, uncalibrated)"
        )
    return out
