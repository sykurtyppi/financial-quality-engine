"""The earnings-night review console: a supervised shadow run, reconciled by hand.

The engine is cleared for supervised shadow runs only: "use the first
earnings runs in supervised shadow mode and manually reconcile every
surfaced fact to its accession before relying on it" (the external audit
that cleared it). This module is what the web console (`/review`) reads and
the one thing it writes. It changes nothing the engine does.

What it reads, never writes: the watchlist, the journal entries, their
pending markers and ``reported`` stamps, and each case's live run through
`report_files.read_live`, which resolves the live pointer ONCE, so the
report, the evidence ledger and the audit shown together are one
generation's. A ledger or audit beside the report that names another run
(``LiveRun.stale``) is said as such and never shown as this run's: the
reconciliation table is built only from the run's own ledger.

What it writes: the reviewer's ticks, one sidecar per case,
``journal/reviews/<TICKER>_<DAY>.review.json``, beside the private entries
(and ignored by git with them). A tick is bound to the run it was made on,
its ``generation_id``, and to the ledger row's id. Row ids are
content-derived (`reporting.ledger._id`), so a rebuild on the same facts has
the same rows: a tick keyed by row alone would read as a check of the new
run, which nobody made. Ticks of an earlier run stay in the file, are
counted apart ("N ticks recorded for an earlier run"), and apply again if
that run is restored. A tick for a run that is not live is refused.

The write is held to the journal entries' rules (Hermes audit of 424b0b4,
finding 5, and the store's durable write): the entry lock's sidecar
(``O_NOFOLLOW``) around the read, the change and the write, so two
reviewers ticking at once both land; the file written whole (fsynced, then
renamed; the directory fsynced); the ``reviews`` folder refused when it is a
symlink (`report_files.own_dir`); and whatever is at the file's name that is
not a regular file, or does not parse as this case's review file, is
refused rather than replaced: a reviewer's record is never overwritten by a
tick that could not read it.
"""

from __future__ import annotations

import csv
import errno
import io
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from app.schemas.ledger import (
    EvidenceItem,
    LedgerDocument,
    Plane,
    Provenance,
    Unsourced,
)
from app.services.journal import reporting, store
from app.services.reporting import report_files
from app.services.watch import watchlist as wl

FORMAT = "fqe-review/1"
# How long a tick waits for a publish of its case to commit (a commit takes
# a moment; a build is never under the lock): a stalled holder must not hold
# the web worker the tick runs on (review of 68dbc24, L-2).
PUBLISH_WAIT_S = 5.0
STATES = ("unchecked", "reconciled", "disputed")
NOTE_MAX = 2000

_GID_RE = re.compile(r"^[0-9a-f]{32}$")
_KEY_RE = re.compile(r"^EV-[0-9a-f]{10}$")  # `reporting.ledger._id`
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
# A document link of an offering row (`reporting.ledger._edgar_url`): the one
# place a ledger written before `LedgerDocument.cik` carried the company's
# CIK, and one more a ledger's own CIK must agree with.
_CIK_URL_RE = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/([0-9]{1,10})/")
# `<stamp>_<seq>_<id>`: a generation directory's name (`report_files._name_next`).
_BUILT_RE = re.compile(r"^(\d{8}T\d{6}Z)_\d+_")

log = logging.getLogger(__name__)


class Refused(Exception):
    """A request the console refuses, with the HTTP ``status`` that says why
    (400 malformed, 404 nothing there, 409 not this run / not safe to write,
    500 the disk failed) and a ``message`` for the reviewer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Unreadable(Exception):
    """The case's review file is there but cannot be taken as its ticks."""


@dataclass(frozen=True)
class Tick:
    state: str
    note: str
    at: str


Runs = dict[str, dict[str, Tick]]  # generation id -> row key -> tick


def case_names(ticker: str, day: str | None) -> tuple[str, str]:
    """The case's ticker and day, validated as the journal validates its
    file names (`store.safe_ticker` / `safe_day`), and the day a real one."""
    try:
        t = store.safe_ticker(ticker)
        d = store.safe_day(day or "")
        date.fromisoformat(d)
    except ValueError as e:
        raise Refused(400, str(e)) from None
    return t, d


def case_day(ticker: str, day: str | None) -> tuple[str, str]:
    """`case_names`, where no day means the ticker's newest journal entry."""
    if day:
        return case_names(ticker, day)
    try:
        path = store.find_entry(ticker)
    except ValueError as e:
        raise Refused(400, str(e)) from None
    if path is None:
        raise Refused(404, f"{ticker!r} has no journal entry; name the day (?date=YYYY-MM-DD)")
    return case_names(ticker, path.stem.split("_", 1)[1])


def reviews_dir() -> Path:
    """Beside the entries, in the journal's own folder."""
    return store.ENTRIES.parent / "reviews"


def review_path(ticker: str, day: str) -> Path:
    t, d = case_names(ticker, day)
    return reviews_dir() / f"{t}_{d}.review.json"


# --- the ticks -----------------------------------------------------------------------


def _load(path: Path, ticker: str, day: str) -> Runs:
    """The ticks recorded for the case, {} when it has no review file. Fails
    closed: something at the name that is not a regular file (a symlink is
    never followed), or that is not this case's review file, raises
    `Unreadable`, and nothing is written over it."""
    name = f"the review file {path.name}"
    try:
        report_files.own_dir(path.parent)
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Unreadable(f"{name} cannot be read: {e}") from None
    if not stat.S_ISREG(mode):
        kind = "a symlink" if stat.S_ISLNK(mode) else "something else"
        raise Unreadable(f"{name} cannot be read: it is not a regular file but {kind}, which "
                         "is never followed; check it, then remove it by hand")
    try:
        # O_NONBLOCK: a FIFO swapped in since the check opens without waiting
        # for a writer, and is refused below.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise Unreadable(f"{name} cannot be read: it is not a regular file")
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Unreadable(f"{name} cannot be read: {e}") from None
    return _parse(raw, ticker, day, name)


def _parse(raw: bytes, ticker: str, day: str, name: str) -> Runs:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as e:  # UnicodeDecodeError is one
        raise Unreadable(f"{name} cannot be read: it is not JSON ({e})") from None
    except RecursionError:  # review of 68dbc24, N-1
        raise Unreadable(f"{name} cannot be read: it is nested too deeply to be "
                         "a review file") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise Unreadable(f"{name} cannot be read: it is not a review file of this console "
                         f"(format {FORMAT})")
    if doc.get("ticker") != ticker or doc.get("day") != day:
        raise Unreadable(f"{name} cannot be read as this case's: it names "
                         f"{doc.get('ticker')!r} {doc.get('day')!r}")
    runs = doc.get("runs")
    if not isinstance(runs, dict):
        raise Unreadable(f"{name} cannot be read: it holds no runs")
    out: Runs = {}
    for gid, rows in runs.items():
        if not (_GID_RE.match(gid) and isinstance(rows, dict)):
            raise Unreadable(f"{name} cannot be read: {gid!r} is not a run's ticks")
        out[gid] = {}
        for key, t in rows.items():
            if not (_KEY_RE.match(key) and isinstance(t, dict) and t.get("state") in STATES
                    and isinstance(t.get("note"), str) and isinstance(t.get("at"), str)):
                raise Unreadable(f"{name} cannot be read: run {gid}, row {key!r} is not a tick")
            out[gid][key] = Tick(t["state"], t["note"], t["at"])
    return out


def _dump(ticker: str, day: str, runs: Runs) -> str:
    doc = {"format": FORMAT, "ticker": ticker, "day": day,
           "runs": {gid: {key: {"state": t.state, "note": t.note, "at": t.at}
                          for key, t in rows.items()} for gid, rows in runs.items()}}
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def _check_note(note: str) -> str:
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise Refused(400, f"the note is {len(note)} characters; at most {NOTE_MAX}")
    if any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in note):
        raise Refused(400, "the note holds control characters")
    return note


def _write_ticks(path: Path, ticker: str, day: str, runs: Runs, generation_id: str, key: str,
                 state: str, tick: Tick) -> None:
    """Write the ticks; the caller holds the locks. A write that raised is
    read back (still under the locks, so no other tick is read for this
    one), as a stamp is (`store.reported_on_disk`): the write renames the
    file into place, then fsyncs its folder, and a failed fsync raises with
    the change made. A reset (only ever written for a row that had a tick)
    landed when the row is gone."""
    try:
        store._durable_write(path, _dump(ticker, day, runs))
    except OSError as e:
        try:
            now = _load(path, ticker, day).get(generation_id, {})
        except Unreadable:
            landed = False
        else:
            landed = key not in now if state == "unchecked" else now.get(key) == tick
        if landed:
            raise Refused(500, f"The tick is recorded, but its write raised after it was in "
                          f"place ({e}): it could not be confirmed durable. Check the "
                          "disk.") from None
        raise Refused(500, f"The tick could not be recorded: {e}") from None


def record_tick(ticker: str, day: str, generation_id: str, key: str, state: str,
                note: str) -> tuple[str, str]:
    """Record the reviewer's ``state`` and ``note`` for row ``key`` of the run
    ``generation_id`` of the case. Returns the case's (ticker, day).

    Refused (`Refused`, nothing written) unless every input is well formed,
    the run is the case's live one, its ledger is its own, and ``key`` is a
    row of that ledger with a filing to check. A reset to "unchecked" of a
    row with no tick is no change, and writes nothing.

    The run is checked twice: first, before anything is written; then, with
    the case's `report_files.publish_lock` held, and inside it the review
    file's lock, across the re-check and the write (reviews of 2f26846,
    finding 3, and efb8500, M1: the review file's lock alone is one no
    publisher takes, so a rebuild could commit between the re-check and the
    write and the tick land on a run no longer live, said as recorded). A
    rebuild that committed since the page was loaded is refused (409); one
    that comes to commit while the tick is written waits the moment it
    takes, and publishes after it, so the tick is always on the run that was
    live when it was written. The publish lock covers only a publish's
    commit (`report_files.replacing` builds before taking it), so a tick
    never waits for a build; it waits for a commit at most
    ``PUBLISH_WAIT_S``, then is refused (503) with nothing written. Lock order: publish lock, then review lock.
    Nothing that holds the review lock takes the publish lock, and a
    publisher (`replacing`, `restore`, `set_aside`) takes only journal
    locks of its own that are never the review file's."""
    t, d = case_names(ticker, day)
    if not _GID_RE.match(generation_id):
        raise Refused(400, f"{generation_id!r} is not a generation id")
    if not _KEY_RE.match(key):
        raise Refused(400, f"{key!r} is not a ledger row id")
    if state not in STATES:
        raise Refused(400, f"{state!r} is not one of {', '.join(STATES)}")
    note = _check_note(note)
    run = read_run(t, d)
    if run.live.generation_id is None:
        raise Refused(409, f"{t} {d}: {_NO_GENERATION}")
    if run.live.generation_id != generation_id:
        raise Refused(409, f"This tick is for run {generation_id}, but the live run of {t} {d} "
                      f"is {run.live.generation_id}: the report was rebuilt (or restored) "
                      "since the page was loaded. Reload it and check the live run.")
    if run.ledger is None:
        raise Refused(409, f"{t} {d}: {' '.join(run.problems)}")
    if key not in {i.id for i in run.ledger.items if _reconcilable(i)}:
        raise Refused(404, f"{key} is not a row with a filing to check in the ledger of run "
                      f"{generation_id}")
    path = reviews_dir() / f"{t}_{d}.review.json"
    tick = Tick(state, note, store.now_iso())
    report = reporting.report_path(t, d)
    try:
        if state == "unchecked" and key not in _load(path, t, d).get(generation_id, {}):
            return t, d  # nothing to reset: no write, no review file for a no-op
        report_files.own_dir(path.parent).mkdir(parents=True, exist_ok=True)
        # The case's publish lock, then the entries' own lock (an O_NOFOLLOW
        # sidecar) on the review file, held across the re-check and the
        # write: see the docstring.
        with report_files.publish_lock(report, timeout=PUBLISH_WAIT_S), \
                store._entry_lock(path):
            now_live = report_files.read_live(report)
            if now_live is None or now_live.generation_id != generation_id:
                raise Refused(409, f"This tick is for run {generation_id}, but the live run of "
                              f"{t} {d} is now "
                              f"{now_live.generation_id if now_live else 'none'}: the report was "
                              "rebuilt while the tick was recorded. Reload the page and check "
                              "the live run.")
            runs = _load(path, t, d)
            rows = runs.setdefault(generation_id, {})
            if state == "unchecked":
                # A reset is no tick (review of 2f26846, finding 4): kept, it
                # read as review work on this run once the run was rebuilt.
                if key not in rows:
                    return t, d  # reset meanwhile: nothing left to write
                rows.pop(key)
                if not rows:
                    del runs[generation_id]
            else:
                rows[key] = tick
            _write_ticks(path, t, d, runs, generation_id, key, state, tick)
    except Unreadable as e:
        raise Refused(409, f"{e}. Not writing over it.") from None
    except report_files.PublishBusy:  # an OSError: said before those
        raise Refused(503, f"A publish of {t} {d} is in progress (its lock has been held for "
                      f"over {PUBLISH_WAIT_S:g}s); nothing was recorded. Retry in a moment, "
                      "then reload: the live run may have changed.") from None
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and e.errno == errno.ELOOP:
            raise Refused(409, f"Not recorded: {e.strerror or e} (a symlink is never "
                          "followed; check it, then remove it by hand)") from None
        raise Refused(500, f"The tick could not be recorded: {e}") from None
    return t, d


# --- one run, whole ------------------------------------------------------------------

_NO_GENERATION = ("this run predates generations: it has no generation id, so a tick could "
                  "not be bound to it. It is shown read-only; rebuild the report to review it.")


@dataclass
class Run:
    """A case's live run, pinned: ``ledger`` is its own or None (``problems``
    says why), and its audit is ``audit`` ("matches", "stale", "none")."""

    live: report_files.LiveRun
    ledger: LedgerDocument | None
    problems: list[str]
    audit: str
    audit_text: str | None
    stale_audit: str | None  # the run a stale audit names
    built: str


def _built(live: report_files.LiveRun) -> str:
    if live.generation_dir is None:
        return "before generations"
    m = _BUILT_RE.match(live.generation_dir.name)
    if m is None:
        return "unknown"
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M:%S UTC")


def read_run(ticker: str, day: str) -> Run:
    """The case's live run through `report_files.read_live`, never its live
    names one by one: they can move between two reads."""
    try:
        live = report_files.read_live(reporting.report_path(ticker, day))
    except (OSError, ValueError) as e:
        # ValueError: a report or audit that is not UTF-8 (review of 2f26846,
        # finding 2), said here rather than raised through the page.
        raise Refused(500, f"The live run of {ticker} {day} cannot be read: "
                      f"{type(e).__name__}: {e}") from None
    if live is None:
        raise Refused(404, f"{ticker} {day} has no live report: nothing to review yet.")
    problems: list[str] = []
    stale = {p.name: report_files.generation_of(p) for p in live.stale}
    this = live.generation_id
    ledger: LedgerDocument | None = None
    if live.ledger is not None:
        try:
            ledger = LedgerDocument.model_validate_json(live.ledger.read_text())
        except (OSError, ValueError, ValidationError) as e:
            problems.append(f"The evidence ledger of this run cannot be read ({e}); there is "
                            "nothing to reconcile against.")
        else:
            if ledger.generation_id != this:  # read_live matched it; a second look costs nothing
                problems.append(f"The evidence ledger names generation {ledger.generation_id}, "
                                f"not this run's {this}; its facts are not shown.")
                ledger = None
    else:
        other = [(n, g) for n, g in stale.items() if n.endswith(".ledger.json")]
        if other:
            problems.append(f"The evidence ledger beside this report names generation "
                            f"{other[0][1]}, not this run's {this}: it is not this run's "
                            "(another run's), so its facts are neither shown nor reconciled "
                            "here. Rebuild or restore the run to review it.")
        else:
            problems.append("This run has no evidence ledger: there is nothing to reconcile.")
    audit, audit_text, stale_audit = "none", None, None
    if live.audit is not None:
        try:
            audit_text = live.audit.read_text()
            audit = "matches"
        except (OSError, ValueError) as e:
            problems.append(f"The audit of this run cannot be read: {e}")
    else:
        audits = [g for n, g in stale.items() if n.endswith("_audit.md")]
        if audits:
            audit, stale_audit = "stale", audits[0] or "none (from before generations)"
    return Run(live, ledger, problems, audit, audit_text, stale_audit, _built(live))


# --- the case page -------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One source of a row, as the reviewer checks it."""

    text: str  # form, filed, concept, period, value, role
    accession: str | None
    url: str | None  # the filing's folder on EDGAR, when the CIK is known


@dataclass
class Row:
    key: str
    kind: str
    subject: str
    period: str
    value: str
    claim: str
    change_state: str | None
    note: str | None
    sources: list[Source]
    tick: Tick | None
    # False for a row resting on the operator's market observation: no
    # filing to reconcile it to, so no tick is offered or recorded.
    reconcilable: bool = True
    # The value's unit, shown beside it: the row's currency, else the one
    # unit its filed facts share ("shares"); None when the ledger states
    # none (Hermes re-audit of #118 @ 34836cf).
    unit: str | None = None


@dataclass
class Case:
    ticker: str
    day: str
    run: Run
    rows: list[Row] = field(default_factory=list)
    derived: list[EvidenceItem] = field(default_factory=list)
    unsourced: list[Unsourced] = field(default_factory=list)
    # The valuation shadow card's rows (`Plane.VALUATION`), listed apart:
    # unscored, never validated, and the price row not reconcilable at all.
    valuation: list[Row] = field(default_factory=list)
    valuation_derived: list[EvidenceItem] = field(default_factory=list)
    earlier: list[tuple[str, int]] = field(default_factory=list)  # (run, ticks)
    ticks_error: str | None = None
    cik: int | None = None
    cik_note: str | None = None

    @property
    def generation_id(self) -> str | None:
        return self.run.live.generation_id

    @property
    def engine(self) -> str:
        if self.run.ledger is not None and self.run.ledger.engine_commit:
            return self.run.ledger.engine_commit
        m = re.search(rf"^{re.escape(report_files.ENGINE_LINE)}(.+)$", self.run.live.text, re.M)
        return m.group(1).strip() if m else "not stated"

    @property
    def tickable(self) -> bool:
        return (self.generation_id is not None and self.run.ledger is not None
                and self.ticks_error is None)

    @property
    def why_not_tickable(self) -> str | None:
        # A run without its own ledger has no table; its problems say why.
        if self.generation_id is None:
            return _NO_GENERATION
        if self.ticks_error is not None:
            return "its review file cannot be read (above); fix it first."
        return None

    def count(self, state: str) -> int:
        return sum(1 for r in self.rows if r.tick is not None and r.tick.state == state)


def fmt_value(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}"


def _cik(ledger: LedgerDocument) -> tuple[int | None, str | None]:
    """The company's CIK, or None and why: the ledger's own (`cik`, the run's
    resolved CIK, withheld when any payload named a different one), else — a
    ledger written before it — an offering's document link. Never guessed
    from an accession, whose prefix is whoever filed it (often a filing
    agent), and never taken from a link when the ledger withheld it; a link
    naming another CIK than the ledger's links nothing."""
    found = {int(m.group(1)) for i in ledger.items for p in i.provenance
             if p.url and (m := _CIK_URL_RE.match(p.url))}
    if ledger.cik is not None:
        found.add(ledger.cik)
    elif ledger.cik_note:
        return None, (f"This run's ledger withholds the company's CIK: {ledger.cik_note}. "
                      "Accessions are shown as text: look each one up on EDGAR by accession "
                      "number.")
    if len(found) == 1:
        return found.pop(), None
    if not found:
        return None, ("This run's ledger names no CIK, so accessions are shown as text: look "
                      "each one up on EDGAR by accession number.")
    return None, (f"This run's ledger names more than one CIK ({', '.join(map(str, sorted(found)))}), "
                  "so accessions are shown as text.")


def _source(p: Provenance, cik: int | None) -> Source:
    if p.kind == "snapshot":
        return Source(f"companyfacts snapshot sha256 {(p.snapshot_sha256 or '')[:12]}… "
                      f"captured {p.captured}" + (f" · {p.role}" if p.role else ""), None, None)
    if p.kind == "observation":
        # The operator's own record: no accession, no EDGAR folder.
        observed = p.observed_at.isoformat() if p.observed_at else "?"
        return Source(f"market observation · {p.source} · observed {observed}"
                      + (f" · {p.role}" if p.role else ""), None, None)
    period = (f"{p.period_start} → {p.period_end}" if p.period_start
              else str(p.period_end) if p.period_end else None)
    # The value as filed, what the reviewer finds in the filing; whether the
    # metric subtracted it is said beside it, not folded into its sign.
    bits = [f"{p.form} filed {p.filed}", p.concept, period,
            None if p.value is None else " ".join(b for b in (fmt_value(p.value), p.unit) if b),
            "subtracted" if p.sign == -1 else None, p.method, p.role]
    url = None
    if cik is not None and p.accession and _ACCESSION_RE.match(p.accession):
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{p.accession.replace('-', '')}/"
    return Source(" · ".join(b for b in bits if b), p.accession, url)


def case(ticker: str, day: str | None) -> Case:
    """One case: its live run, pinned, and the reconciliation of its ledger
    with the ticks recorded for that run."""
    t, d = case_day(ticker, day)
    run = read_run(t, d)
    c = Case(t, d, run)
    try:
        runs = _load(reviews_dir() / f"{t}_{d}.review.json", t, d)
    except Unreadable as e:
        runs, c.ticks_error = {}, f"{e}. No tick is shown, and none is recorded until it is fixed."
    gid = run.live.generation_id
    mine = runs.get(gid, {}) if gid is not None else {}
    # Resets are not ticks; a file from before they were dropped may hold some.
    counted = {g: sum(1 for x in rows.values() if x.state != "unchecked") for g, rows in runs.items()}
    c.earlier = [(g, n) for g, n in counted.items() if g != gid and n]
    if run.ledger is None:
        return c
    c.cik, c.cik_note = _cik(run.ledger)
    for item in run.ledger.items:
        shadow = item.plane is Plane.VALUATION
        if not item.provenance:
            (c.valuation_derived if shadow else c.derived).append(item)
            continue
        row = Row(
            key=item.id, kind=item.kind, subject=item.subject, period=item.fiscal_label or "—",
            value=fmt_value(item.value), claim=item.claim, change_state=item.change_state,
            note=item.note, sources=[_source(p, c.cik) for p in item.provenance],
            tick=mine.get(item.id), reconcilable=_reconcilable(item), unit=_unit(item))
        (c.valuation if shadow else c.rows).append(row)
    c.unsourced = list(run.ledger.unsourced)
    return c


def _unit(item: EvidenceItem) -> str | None:
    """The unit a row's value is in: its currency, else the one unit its
    filed facts share; None when the ledger states none or they differ."""
    if item.currency:
        return item.currency
    units = {p.unit for p in item.provenance if p.kind == "filing"}
    return units.pop() if len(units) == 1 else None


def _reconcilable(item: EvidenceItem) -> bool:
    """A row with a filing to check against: one resting on the market
    observation has none (the operator's own record is not a document)."""
    return bool(item.provenance) and not any(p.kind == "observation" for p in item.provenance)


# --- the export ----------------------------------------------------------------------

_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_CSV_FIELDS = ("ticker", "day", "generation_id", "key", "kind", "subject", "period", "value",
               "change_state", "accessions", "state", "note", "recorded_at")


def _cell(text: str) -> str:
    """A text cell a spreadsheet will not run as a formula."""
    return "'" + text if text.startswith(_FORMULA) else text


def _md(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def export(c: Case, fmt: str) -> str:
    """The case's reconciliation for the shadow-run log: CSV (one line per
    row) or Markdown (the run, the counts, the rows, the unsourced)."""
    if c.run.ledger is None:
        raise Refused(409, f"{c.ticker} {c.day}: {' '.join(c.run.problems)}")
    gid = c.generation_id or ""
    lines = []
    rows = [*c.rows, *c.valuation]
    for r in rows:
        tick = r.tick or Tick("unchecked", "", "")
        lines.append({"ticker": c.ticker, "day": c.day, "generation_id": gid, "key": r.key,
                      "kind": r.kind, "subject": _cell(r.subject), "period": r.period,
                      "value": r.value, "change_state": _cell(r.change_state or ""),
                      "accessions": "; ".join(dict.fromkeys(s.accession for s in r.sources
                                                            if s.accession)),
                      "state": tick.state, "note": _cell(tick.note), "recorded_at": tick.at})
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(lines)
        return out.getvalue()
    md = [f"# Shadow-run review: {c.ticker} {c.day}", "",
          f"- Run: generation {gid or 'none (before generations)'}, built {c.run.built}",
          f"- Engine: {c.engine}",
          f"- Audit: {_audit_words(c.run)}",
          f"- Exported: {store.now_iso()}",
          f"- {c.count('reconciled')} of {len(c.rows)} rows reconciled, "
          f"{c.count('disputed')} disputed",
          *[f"- {n} tick{'s' if n != 1 else ''} recorded for an earlier run {g} (not this run's)"
            for g, n in c.earlier],
          "",
          "| row | subject | period | value | change | accessions | state | note |",
          "|---|---|---|---|---|---|---|---|"]
    def table(pairs: list[tuple[dict, Row]]) -> list[str]:
        return [f"| {x['key']} | {_md(r.subject)} | {_md(r.period)} | {x['value']} | "
                f"{_md(r.change_state or '')} | {x['accessions']} | {x['state']} | "
                f"{_md(r.tick.note if r.tick else '')} |" for x, r in pairs]

    pairs = list(zip(lines, rows))
    md += table(pairs[:len(c.rows)])
    if c.valuation:
        md += ["", "## Valuation (shadow)", "",
               "_Not scored, never validated; the price row rests on the operator's market "
               "observation and has no filing to check._", "",
               "| row | subject | period | value | change | accessions | state | note |",
               "|---|---|---|---|---|---|---|---|"]
        md += table(pairs[len(c.rows):])
    md += ["", "## No document to check against", ""]
    md += [f"- {_md(u.kind)} {_md(u.subject)}: {_md(u.claim)} ({_md(u.reason)})"
           for u in c.unsourced] or ["- none"]
    return "\n".join(md) + "\n"


def _audit_words(run: Run) -> str:
    if run.audit == "matches":
        return "present, of this run"
    if run.audit == "stale":
        return f"stale: it names run {run.stale_audit}, not this one"
    return "none yet"


# --- the board -----------------------------------------------------------------------


@dataclass
class BoardRow:
    ticker: str
    day: str | None
    watch: wl.Watch | None = None
    entry: str = "none pinned"  # "v1", "v2", "missing", "none pinned"
    report: str = "none"  # "none", "live", "unreadable"
    problem: str | None = None
    generation_id: str | None = None
    built: str | None = None
    pending: store.Pending | None = None
    stamped: str | None = None
    audit: str = "none"
    stale_audit: str | None = None
    ledger_ok: bool = False
    total: int = 0
    reconciled: int = 0
    disputed: int = 0
    earlier: int = 0


def _board_row(ticker: str, day: str | None, watch: wl.Watch | None) -> BoardRow:
    row = BoardRow(ticker, day, watch)
    if day is None:
        return row
    try:
        t, d = case_names(ticker, day)
    except Refused as e:
        row.problem = e.message
        return row
    path = store.entry_path(t, d)
    row.entry = ("v2" if store.is_v2(path) else "v1") if path.exists() else "missing"
    row.pending = store.pending_marker(path)
    row.stamped = store.reported_on_disk(path)
    try:
        c = case(t, d)
    except Refused as e:
        if e.status != 404:
            row.report, row.problem = "unreadable", e.message
        return row
    row.report, row.generation_id, row.built = "live", c.generation_id, c.run.built
    row.audit, row.stale_audit = c.run.audit, c.run.stale_audit
    row.ledger_ok = c.run.ledger is not None
    row.problem = " ".join(filter(None, [*c.run.problems, c.ticks_error])) or None
    row.total, row.reconciled, row.disputed = len(c.rows), c.count("reconciled"), c.count("disputed")
    row.earlier = sum(n for _, n in c.earlier)
    return row


def _isolated_row(ticker: str, day: str | None, watch: wl.Watch | None) -> BoardRow:
    """`_board_row`, where anything one case raises is that row's problem:
    one unreadable case never takes the board down (review of 2f26846,
    finding 2)."""
    try:
        return _board_row(ticker, day, watch)
    except Exception as e:  # noqa: BLE001 - said on its row, never a board 500
        # Logged with its traceback: an error nobody foresaw is a bug, and
        # the row's one line is no place to find it (review of efb8500, L3).
        log.exception("review board: case %s %s cannot be read", ticker, day)
        return BoardRow(ticker, day, watch, report="unreadable",
                        problem=f"This case cannot be read: {type(e).__name__}: {e}")


def board() -> tuple[list[BoardRow], list[str]]:
    """The night's cases: every watchlist name (its pinned entry's case, if
    any), then every journal entry with a live report or a pending one.
    Read-only. Returns the rows and what could not be read."""
    problems: list[str] = []
    rows: list[BoardRow] = []
    seen: set[tuple[str, str | None]] = set()
    try:
        watches = wl.load()
    except (wl.WatchlistError, OSError) as e:
        # What the loader refuses, or the disk: said, and logged in one line
        # (a traceback on every refresh of the board is noise; review of
        # 68dbc24, N-3).
        log.warning("review board: the watchlist cannot be read: %s", e)
        watches = []
        problems.append(f"The watchlist cannot be read: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 - said on the board, never a board 500
        # Anything else is a bug (review of efb8500, L2): said, and logged
        # with its traceback.
        log.exception("review board: the watchlist cannot be read")
        watches = []
        problems.append(f"The watchlist cannot be read: {type(e).__name__}: {e}")
    for w in watches:
        seen.add((w.ticker, w.thesis_entry))
        rows.append(_isolated_row(w.ticker, w.thesis_entry, w))
    journal: list[BoardRow] = []
    for p in store.list_entries():
        ticker, _, day = p.stem.partition("_")
        if (ticker, day) in seen:
            continue
        row = _isolated_row(ticker, day, None)
        if row.report != "none" or row.pending is not None:
            journal.append(row)
    journal.sort(key=lambda r: (r.day or "", r.ticker), reverse=True)
    return rows + journal, problems
