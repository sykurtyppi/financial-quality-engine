"""The earnings calendar the nudge and poller read from.

Deliberately NOT a store of views. It holds only timing — which name reports
when — because the BEFORE thesis is always the analyst's, written blind
(journal/JOURNAL.md rule 1). Keeping the calendar and the priors in separate
files is what lets the calendar be shared/committed while the priors stay
private in the gitignored `journal/entries/`.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.services.journal import store

ROOT = Path(__file__).resolve().parents[3]
WATCHLIST = ROOT / "journal" / "watchlist.json"

# The forms that carry the XBRL financials the engine actually needs. An 8-K
# earnings release lands first and is picked up as a document, but it is the
# periodic report that makes the quarter analyzable.
DEFAULT_FORMS = ("10-Q", "10-K")

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WatchlistError(ValueError):
    """Malformed watchlist. Raised with the offending entry named."""


@dataclass(frozen=True)
class Watch:
    """One name on the calendar.

    ``print_at`` is a SCHEDULING HINT ONLY — it decides when polling starts,
    never which filing counts. Filing identity comes from the event fields:
    ``baseline_accession`` (the newest qualifying accession at arm time; a new
    accession beyond it is what triggers) and ``expected_report_date`` (the
    fiscal period the event is for, so an intervening earlier quarter's filing
    cannot hijack the watch). A watch without both fields cannot poll — using
    a forecast date as a filing cutoff was how an early filing got ignored
    forever.

    ``thesis_entry``/``thesis_sha256`` pin the exact journal entry (and its
    BEFORE-block lock hash) that belongs to THIS event, so a stale prior
    quarter's thesis can never authorize this quarter's report.
    """

    ticker: str
    print_at: datetime
    label: str | None = None
    forms: tuple[str, ...] = DEFAULT_FORMS
    note: str | None = None
    baseline_accession: str | None = None  # "" = no prior qualifying filing existed
    expected_report_date: date | None = None
    thesis_entry: str | None = None  # YYYY-MM-DD of the pinned journal entry
    thesis_sha256: str | None = None  # v2 before_sha256 at link time

    @property
    def event_armed(self) -> bool:
        """True when the watch carries the identity fields that make filing
        detection safe. Legacy rows without them fail closed in the poller."""
        return self.baseline_accession is not None and self.expected_report_date is not None

    def hours_until(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.print_at - now).total_seconds() / 3600.0

    def is_before_print(self, now: datetime | None = None) -> bool:
        return self.hours_until(now) > 0


def _parse_dt(raw: object, ticker: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise WatchlistError(f"{ticker}: print_at must be an ISO-8601 string, got {raw!r}")
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError as e:
        raise WatchlistError(f"{ticker}: unparseable print_at {raw!r} ({e})") from e
    if dt.tzinfo is None:
        # A naive timestamp here silently means "whatever timezone the box is
        # in", which for a US-market print on a UTC server is an off-by-hours
        # error in the one field the whole schedule turns on.
        raise WatchlistError(
            f"{ticker}: print_at {raw!r} has no timezone. Use an explicit offset, "
            f'e.g. "2026-08-26T20:20:00Z" (AMC prints are ~20:20Z in EDT).'
        )
    return dt.astimezone(timezone.utc)


def parse_watch(raw: dict) -> Watch:
    if not isinstance(raw, dict):
        raise WatchlistError(f"each watchlist item must be an object, got {type(raw).__name__}")
    try:
        ticker = store.safe_ticker(raw.get("ticker", ""))
    except ValueError as e:
        raise WatchlistError(f"invalid ticker in watchlist: {e}") from e

    forms_raw = raw.get("forms", DEFAULT_FORMS)
    if isinstance(forms_raw, str) or not all(isinstance(f, str) for f in forms_raw):
        raise WatchlistError(f"{ticker}: forms must be a list of strings, got {forms_raw!r}")
    forms = tuple(f.strip().upper() for f in forms_raw if f.strip())
    if not forms:
        raise WatchlistError(f"{ticker}: forms is empty — nothing could ever trigger")

    baseline = raw.get("baseline_accession")
    if baseline is not None and not isinstance(baseline, str):
        raise WatchlistError(f"{ticker}: baseline_accession must be a string, got {baseline!r}")

    expected = raw.get("expected_report_date")
    expected_d: date | None = None
    if expected is not None:
        try:
            expected_d = date.fromisoformat(str(expected))
        except ValueError as e:
            raise WatchlistError(
                f"{ticker}: expected_report_date must be YYYY-MM-DD, got {expected!r}"
            ) from e

    thesis_entry = raw.get("thesis_entry") or None
    thesis_sha = raw.get("thesis_sha256") or None
    if (thesis_entry is None) != (thesis_sha is None):
        # A pin without its hash (or vice versa) cannot be verified — refuse
        # rather than let an unverifiable pin authorize a report.
        raise WatchlistError(
            f"{ticker}: thesis_entry and thesis_sha256 must be set together"
        )
    if thesis_entry is not None and not _DAY_RE.match(str(thesis_entry)):
        raise WatchlistError(f"{ticker}: thesis_entry must be YYYY-MM-DD, got {thesis_entry!r}")
    if thesis_sha is not None and not _SHA256_RE.match(str(thesis_sha)):
        raise WatchlistError(f"{ticker}: thesis_sha256 must be 64 hex chars")

    return Watch(
        ticker=ticker,
        print_at=_parse_dt(raw.get("print_at"), ticker),
        label=(raw.get("label") or None),
        forms=forms,
        note=(raw.get("note") or None),
        baseline_accession=baseline,
        expected_report_date=expected_d,
        thesis_entry=thesis_entry,
        thesis_sha256=thesis_sha,
    )


def load(path: Path | None = None) -> list[Watch]:
    """Read the watchlist. A missing file is an empty watchlist, not an error —
    the poller is opt-in and should not crash a cron job before setup."""
    p = path or WATCHLIST
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise WatchlistError(f"{p}: invalid JSON ({e})") from e

    items = data.get("watchlist") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise WatchlistError(f'{p}: expected a "watchlist" array')

    watches = [parse_watch(item) for item in items]
    dupes = {w.ticker for w in watches if sum(1 for x in watches if x.ticker == w.ticker) > 1}
    if dupes:
        # Two rows for one ticker means one of them silently loses; which one
        # depends on iteration order. Refuse rather than pick.
        raise WatchlistError(f"{p}: duplicate tickers: {', '.join(sorted(dupes))}")
    return sorted(watches, key=lambda w: w.print_at)


@contextmanager
def _write_lock(p: Path):
    """Serialize read-modify-write cycles across processes.

    ``_atomic_write`` prevents a half-written file, but not a lost update: two
    concurrent ``add``/``link`` invocations both read, both modify their own
    copy, and the second rename silently discards the first's change — e.g.
    the thesis pin on a multi-name earnings night. The lock lives on a stable
    sidecar file because ``os.replace`` swaps the watchlist's inode out from
    under any lock held on the file itself.

    Assumes a local POSIX filesystem: ``fcntl.flock`` is not reliable across
    NFS mounts, so do not host ``journal/`` on one and expect this guarantee.
    """
    lock_path = p.with_name(p.name + ".lock")
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def add_entry(raw: dict, path: Path | None = None) -> Watch:
    """Validate one entry and append it to the watchlist file.

    Validation runs BEFORE the write (parse_watch raises on anything the
    loader would later choke on), so a bad `add` can never corrupt the file a
    cron job reads. Existing content — including the `_comment` block — is
    preserved as-is.
    """
    watch = parse_watch(raw)
    p = path or WATCHLIST
    with _write_lock(p):
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                raise WatchlistError(f"{p}: invalid JSON ({e})") from e
            if isinstance(data, list):
                data = {"watchlist": data}
        else:
            data = {"watchlist": []}
        items = data.setdefault("watchlist", [])
        if any(
            isinstance(it, dict) and str(it.get("ticker", "")).upper() == watch.ticker
            for it in items
        ):
            raise WatchlistError(f"{watch.ticker} is already on the watchlist")
        items.append(raw)
        _atomic_write(p, data)
    return watch


def _atomic_write(p: Path, data: dict) -> None:
    """Temp-file + rename so a crash mid-write can never leave the calendar a
    cron job reads half-written."""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_entry(ticker: str, updates: dict, path: Path | None = None) -> Watch:
    """Merge ``updates`` into the row for ``ticker`` and persist atomically.

    The merged row is re-validated through ``parse_watch`` BEFORE the write,
    so an update can never corrupt the file or persist an inconsistent pin
    (e.g. a thesis_entry without its hash).
    """
    p = path or WATCHLIST
    if not p.exists():
        raise WatchlistError(f"{p}: no watchlist to update")
    with _write_lock(p):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            raise WatchlistError(f"{p}: invalid JSON ({e})") from e
        if isinstance(data, list):
            data = {"watchlist": data}
        items = data.setdefault("watchlist", [])
        t = store.safe_ticker(ticker)
        row = next(
            (it for it in items
             if isinstance(it, dict) and str(it.get("ticker", "")).upper() == t),
            None,
        )
        if row is None:
            raise WatchlistError(f"{t} is not on the watchlist ({p})")
        # None clears a field (re-arming drops the spent pin and label);
        # the row never persists explicit nulls.
        merged = {k: v for k, v in {**row, **updates}.items() if v is not None}
        watch = parse_watch(merged)  # validate before touching the file
        row.clear()
        row.update(merged)
        _atomic_write(p, data)
    return watch


def remove_entry(ticker: str, path: Path | None = None) -> None:
    """Drop the row for ``ticker``. Unknown tickers raise, so a prune that
    thinks it removed something it did not is impossible."""
    p = path or WATCHLIST
    if not p.exists():
        raise WatchlistError(f"{p}: no watchlist to update")
    with _write_lock(p):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            raise WatchlistError(f"{p}: invalid JSON ({e})") from e
        if isinstance(data, list):
            data = {"watchlist": data}
        items = data.setdefault("watchlist", [])
        t = store.safe_ticker(ticker)
        keep = [it for it in items
                if not (isinstance(it, dict) and str(it.get("ticker", "")).upper() == t)]
        if len(keep) == len(items):
            raise WatchlistError(f"{t} is not on the watchlist ({p})")
        data["watchlist"] = keep
        _atomic_write(p, data)


def due(watches: list[Watch], within_hours: float, now: datetime | None = None) -> list[Watch]:
    """Names whose print is inside the window and still ahead of us.

    Past prints drop out: the nudge exists to catch you BEFORE the print, and a
    reminder to write a blind prior on a quarter already reported is worse than
    no reminder — it invites a thesis written with the tape already visible.
    """
    now = now or datetime.now(timezone.utc)
    return [w for w in watches if 0 < w.hours_until(now) <= within_hours]
