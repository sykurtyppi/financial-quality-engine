"""What the workbench pages show, read from the files a run publishes.

Read-only, and only the workbench's own runs: they are published under
``reports/workbench/`` (`reports_dir`), never at the journal's live names in
``reports/`` nor the auto track's in ``reports/auto/`` (independent review
of 9d00328, H1+H2: a workbench run published over a journal case pending
its audit, and replaced an audited case's live run in the review console).
A ticker's runs are its reports' generations
(``reports/workbench/.generations/<T>_<day>/<stamp>_<seq>_<id>/``), listed by
`report_files.generations` and read by `report_files.read_live`, which pins
one generation for the report, its ledger and its audit together: a page
never shows one run's card beside another run's ledger. A past run is found
by matching the id in the URL against the ticker's own listed generations,
never by joining it into a path, so nothing outside them can be named.

Flag counts per tier are read from the card. The evidence ledger holds the
run's metrics (each `directional`) and events, not the card's flags: a run
with two Tier-2 flags has dozens of directional ledger rows, so counting
them would count evidence, not flags.

Anything that cannot be read is a problem said on the page, never a 500:
one unreadable run must not take the ticker, or the watchlist, down.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.schemas.ledger import LedgerDocument
from app.services.brief import sources as brief_sources
from app.services.journal import reporting, store
from app.services.reporting import report_files
from app.services.valuation.observation import LoadedObservation, find_observation
from app.services.watch import watchlist as wl

log = logging.getLogger(__name__)

# The workbench's runs, beside the journal's (``reports/``) and the auto
# track's (``reports/auto/``): the same layout, its own live names. The
# journal's and the auto track's readers (`earnings_brief.latest_report`,
# the watch, the review console) never look in it.
WORKBENCH_DIR = "workbench"

# `report_builder.build_report`'s assembly: the card and the scope notice,
# then this, then the appendix.
APPENDIX_MARKER = "\n\n---\n\n# Full report (appendix)\n\n"
# A run older than this is stale on the watchlist even with no print since:
# a week is longer than any filing-night cycle (the market observation's
# own STALE_AFTER_DAYS is the same week, for the same reason).
RUN_STALE_DAYS = 7
GENERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# `<stamp>_<seq>_<id>`: a generation directory's name (`report_files._name_next`).
_GEN_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_(\d+)_([0-9a-f]{32}|adopted)$")
# A live report's name: `<T>_<day>.md`; never an audit, a replay or a brief.
_REPORT_NAME_RE = re.compile(r"^([A-Z0-9][A-Z0-9.\-]{0,11})_(\d{4}-\d{2}-\d{2})\.md$")
_TIER_RE = re.compile(r"^\*\*Tier ([123]) — ")
_NOT_CHECKED = "- ⚠ not checked this run"
_NONE = "- none surfaced this run"


@dataclass(frozen=True)
class TierCounts:
    """The card's attention flags per tier, and its Tier-1 "not checked"
    line apart: a source not checked is not a flag, and not a clean bill."""

    tier1: int
    tier2: int
    tier3: int
    not_checked: int

    @property
    def flags(self) -> int:
        return self.tier1 + self.tier2 + self.tier3


@dataclass(frozen=True)
class RunRef:
    """One published run of a ticker: ``name`` is its generation
    directory (None for files from before generations, read at their live
    names), ``generation_id`` the id that directory carries, ``built``
    when it was published (UTC). ``superseded``: kept, never made live
    (`report_files.Superseded`: it finished after a run asked for later had
    published). ``pending``: it holds `report_files.PENDING_MARK`; not
    live, it was left by a publishing process that stopped before its
    switch, and was never published (review of 6bf9f9e, L5). ``fence``:
    the request number it was sealed with, for a live run (None otherwise,
    or when it states none)."""

    ticker: str
    day: str
    name: str | None
    generation_id: str | None
    built: datetime | None
    live: bool
    report: Path
    superseded: bool = False
    pending: bool = False
    fence: int | None = None

    @property
    def never_published(self) -> bool:
        return self.pending and not self.live


@dataclass(frozen=True)
class LedgerSummary:
    generated_on: str
    coverage: float | None
    fresh: bool
    items: int
    valuation: str | None  # the shadow card's state; None when not requested


@dataclass(frozen=True)
class RunView:
    ref: RunRef
    card: str
    appendix: str
    generation_id: str | None
    engine: str | None
    tiers: TierCounts | None
    ledger: LedgerSummary | None
    ledger_problem: str | None
    audit: Path | None

    @property
    def day(self) -> str:
        return self.ref.day

    @property
    def live(self) -> bool:
        return self.ref.live


@dataclass
class TickerView:
    ticker: str
    latest: RunView | None = None
    # The day of the journal case of this ticker open today, if one is: this
    # page's card is the workbench's, not the case's.
    journal_case: str | None = None
    runs: list[RunRef] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    briefs: list[Path] = field(default_factory=list)
    observation: LoadedObservation | None = None
    observation_error: str | None = None
    watch: wl.Watch | None = None
    watch_error: str | None = None


# --- the card ---------------------------------------------------------------------------


def split_report(text: str) -> tuple[str, str]:
    """(card, appendix): the card with its scope notice, and the appendix
    after its heading. A report without the marker (from before the card)
    is all card."""
    card, marker, appendix = text.partition(APPENDIX_MARKER)
    return (card, appendix) if marker else (text, "")


def tier_counts(card: str) -> TierCounts | None:
    """Bullets under each ``**Tier N — ...:**`` heading of the card, up to
    the next tier or section; None for a card without tiers."""
    counts = {1: 0, 2: 0, 3: 0}
    not_checked = 0
    tier: int | None = None
    seen = False
    for line in card.splitlines():
        m = _TIER_RE.match(line)
        if m:
            tier, seen = int(m.group(1)), True
        elif line.startswith("#"):
            tier = None
        if tier is None or not line.startswith("- "):
            continue
        if line.startswith(_NOT_CHECKED):
            not_checked += 1
        elif not line.startswith(_NONE):
            counts[tier] += 1
    return TierCounts(counts[1], counts[2], counts[3], not_checked) if seen else None


def _engine(text: str) -> str | None:
    m = re.search(rf"^{re.escape(report_files.ENGINE_LINE)}(.+)$", text, re.M)
    return m.group(1).strip() if m else None


# --- a ticker's runs --------------------------------------------------------------------


def reports_dir() -> Path:
    """Where the workbench's runs are published (`jobs` passes it to
    `reporting.build_report` as ``out_dir``) and the only folder its pages
    read. Looked up at call time, under `reporting.REPORTS`."""
    return reporting.REPORTS / WORKBENCH_DIR


def _report(ticker: str, day: str) -> Path:
    return reports_dir() / f"{ticker}_{day}.md"


def _days(ticker: str) -> list[str]:
    """Every day with a live name or a generations folder for ``ticker``,
    newest first."""
    root = reports_dir()
    days: set[str] = set()
    for folder in (root, root / report_files.GENERATIONS_DIR):
        try:
            names = [p.name for p in folder.iterdir()] if folder.is_dir() else []
        except OSError:
            continue
        for name in names:
            m = _REPORT_NAME_RE.match(name if name.endswith(".md") else f"{name}.md")
            if m and m.group(1) == ticker:
                days.add(m.group(2))
    return sorted(days, reverse=True)


def _built(name: str) -> datetime | None:
    m = _GEN_NAME_RE.match(name)
    if m is None:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _refs(ticker: str, day: str) -> list[RunRef]:
    """One day's runs, newest first. Raises OSError (a link where a
    folder of ours should be; a pointer we did not write) for the caller
    to say."""
    report = _report(ticker, day)
    current = report_files.current_generation(report)
    gens = report_files.generations(report)
    refs = []
    for gen in reversed(gens):
        m = _GEN_NAME_RE.match(gen.name)
        gid = m.group(3) if m and m.group(3) != "adopted" else None
        live = current is not None and gen == current
        refs.append(RunRef(ticker, day, gen.name, gid, _built(gen.name), live,
                           gen / report.name, (gen / report_files.SUPERSEDED_MARK).exists(),
                           (gen / report_files.PENDING_MARK).exists(),
                           _live_fence(report, gen) if live else None))
    if not gens and report.is_file():
        # Files from before generations, read at their live names.
        mtime = datetime.fromtimestamp(report.stat().st_mtime, UTC)
        refs.append(RunRef(ticker, day, None, None, mtime, True, report))
    return refs


def _live_fence(report: Path, gen: Path) -> int | None:
    """A live run's fence for ordering the card; one that cannot be read
    orders like a run without one (the page still shows the run)."""
    try:
        return report_files.fence_of(report, gen)
    except OSError:
        return None


def runs(ticker: str, problems: list[str] | None = None) -> list[RunRef]:
    """Every kept run of ``ticker`` across days, newest first (by day, then
    by publish order within the day). What cannot be listed is appended to
    ``problems``."""
    t = store.safe_ticker(ticker)
    out: list[RunRef] = []
    for day in _days(t):
        try:
            out.extend(_refs(t, day))
        except (OSError, ValueError) as e:
            if problems is not None:
                problems.append(f"The runs of {t} {day} cannot be listed: {e}")
    return out


def newest_run(ticker: str) -> RunRef | None:
    """The workbench's newest kept run of ``ticker`` (live or not), or
    None."""
    refs = runs(ticker)
    return refs[0] if refs else None


def card_order(refs: list[RunRef]) -> list[RunRef]:
    """The live runs among ``refs``, the card first: the highest request
    number among those that state one, then the newest day (a run from
    before fences never outranks one that states its fence). Day order
    alone let an older request on a newer day be the card (review of
    6bf9f9e, M1: around midnight a build named its day after its fetch)."""
    live = [r for r in refs if r.live]  # `runs` lists newest day first
    return sorted(live, key=lambda r: -1 if r.fence is None else r.fence, reverse=True)


def live_run(ticker: str) -> RunRef | None:
    """The run the ticker page shows as its card (`card_order`), or None.
    What `journal.py openv2` names when the card was there to read before
    the thesis (review of 2cbba1c, N2: a newer kept run may be one
    superseded, never the card)."""
    order = card_order(runs(ticker))
    return order[0] if order else None


def live_generation(ticker: str) -> str | None:
    """The generation id of the run the ticker page shows (`live_run`;
    None when none is, or it is from before generations). What a finished
    job is compared with: a run that published is not the live one once a
    later run has (Hermes audit of PR #118, finding 1)."""
    ref = live_run(ticker)
    return ref.generation_id if ref is not None else None


def published_since(ticker: str, when: datetime) -> bool:
    """Whether a run of ``ticker`` (any day, kept) was published after
    ``when``: one that is or was live, never one kept as superseded, which
    no reader ever saw as the card (review of 2cbba1c, L2: it hid the
    latest run's failure). A generation's stamp has whole seconds, so only
    one stamped in a later second than ``when``'s counts: one stamped in the
    same second may be from before it, and a failure said once too often is
    better than one hidden by a run that preceded it."""
    second = when.replace(microsecond=0)
    return any(r.built is not None and r.built > second and (r.live or not r.superseded)
               and not r.never_published for r in runs(ticker))


def _ledger(live: report_files.LiveRun) -> tuple[LedgerSummary | None, str | None]:
    if live.ledger is None:
        if any(p.name.endswith(".ledger.json") for p in live.stale):
            return None, "The evidence ledger beside this report is another run's; it is not shown."
        return None, "This run has no evidence ledger."
    try:
        doc = LedgerDocument.model_validate_json(live.ledger.read_text())
    except (OSError, ValueError, ValidationError) as e:
        first = str(e).splitlines()[0]
        return None, f"The evidence ledger of this run cannot be read ({first})."
    return LedgerSummary(
        generated_on=doc.generated_on.isoformat(), coverage=doc.coverage, fresh=doc.fresh,
        items=len(doc.items), valuation=doc.valuation.state if doc.valuation else None), None


def read_run(ref: RunRef) -> RunView | None:
    """The run ``ref`` names, pinned to its generation (`read_live`); None
    when there is nothing to read there."""
    live = report_files.read_live(ref.report)
    if live is None:
        return None
    card, appendix = split_report(live.text)
    ledger, problem = _ledger(live)
    return RunView(ref, card, appendix, live.generation_id, _engine(live.text),
                   tier_counts(card), ledger, problem, live.audit)


def _latest(refs: list[RunRef], day: str | None, problems: list[str]) -> RunView | None:
    """The card's run (`card_order`; of ``day``, when given)."""
    for ref in card_order(refs):
        if day is not None and ref.day != day:
            continue
        try:
            view = read_run(ref)
        except (OSError, ValueError) as e:
            problems.append(f"The live run of {ref.ticker} {ref.day} cannot be read: {e}")
            continue
        if view is not None:
            return view
    return None


def _briefs(ticker: str) -> list[Path]:
    root = brief_sources.BRIEFS
    try:
        names = sorted((p for p in root.iterdir() if p.is_file()), reverse=True) \
            if root.is_dir() else []
    except OSError:
        return []
    return [p for p in names
            if (m := _REPORT_NAME_RE.match(p.name)) and m.group(1) == ticker]


def ticker_view(ticker: str, day: str | None = None) -> TickerView:
    """Everything the ticker page shows. ``ticker`` is validated
    (`store.safe_ticker`, ValueError otherwise)."""
    t = store.safe_ticker(ticker)
    v = TickerView(t)
    v.runs = runs(t, v.problems)
    v.latest = _latest(v.runs, day, v.problems)
    v.briefs = _briefs(t)
    today = date.today().isoformat()  # the journal's own day (`journal.py openv2`)
    try:
        v.journal_case = today if store.find_entry(t, today) is not None else None
    except (OSError, ValueError):
        v.journal_case = None
    try:
        v.observation = find_observation(reporting.MARKET.parent, t)
    except (OSError, ValueError) as e:  # ObservationError is a ValueError
        v.observation_error = str(e)
    try:
        v.watch = next((w for w in wl.load() if w.ticker == t), None)
    except (wl.WatchlistError, OSError) as e:
        v.watch_error = f"The watchlist cannot be read: {e}"
    return v


def past_run(ticker: str, generation_id: str) -> RunView | None:
    """The run of ``ticker`` whose generation id is ``generation_id``, or
    None. Only a 32-hex id is looked up, and only among the ticker's own
    listed generations: the id is matched, never joined into a path."""
    if not GENERATION_ID_RE.match(generation_id):
        return None
    for ref in runs(ticker):
        if ref.generation_id == generation_id:
            return read_run(ref)
    return None


# --- the watchlist and the recent runs -----------------------------------------------


@dataclass(frozen=True)
class WatchRow:
    ticker: str
    print_at: datetime
    label: str | None
    latest: RunRef | None
    tiers: TierCounts | None
    stale: str | None
    problem: str | None = None


def stale_reason(print_at: datetime, built: datetime, now: datetime) -> str | None:
    """Why a ticker's latest run is stale, or None. The watchlist knows one
    thing about the filing: when the print is expected (``print_at``, a
    scheduling hint). A run built before a print that has since passed may
    predate the quarter's filing; any run older than RUN_STALE_DAYS is old
    whatever the calendar says."""
    if print_at <= now and built < print_at:
        return f"built before the print expected {print_at:%Y-%m-%d %H:%M} UTC"
    if now - built > timedelta(days=RUN_STALE_DAYS):
        return f"older than {RUN_STALE_DAYS} days"
    return None


def _card_tiers(ref: RunRef) -> TierCounts | None:
    try:
        view = read_run(ref)
    except (OSError, ValueError):
        return None
    return view.tiers if view is not None else None


def watchlist_rows(now: datetime | None = None) -> tuple[list[WatchRow], str | None]:
    """Each watched ticker with its next print, latest run and that run's
    flag counts; and why the watchlist could not be read, if it could
    not."""
    now = now or datetime.now(UTC)
    try:
        watches = wl.load()
    except (wl.WatchlistError, OSError) as e:
        return [], f"The watchlist cannot be read: {e}"
    rows = []
    for w in watches:
        problems: list[str] = []
        order = card_order(runs(w.ticker, problems))
        latest = order[0] if order else None
        tiers = _card_tiers(latest) if latest is not None else None
        stale = (stale_reason(w.print_at, latest.built, now)
                 if latest is not None and latest.built is not None else None)
        rows.append(WatchRow(w.ticker, w.print_at, w.label, latest, tiers, stale,
                             "; ".join(problems) or None))
    return rows, None


@dataclass(frozen=True)
class RecentRun:
    ref: RunRef
    tiers: TierCounts | None


def recent_runs(limit: int = 20) -> list[RecentRun]:
    """The live run of every ticker-day in ``reports/workbench/``, newest
    published first."""
    root = reports_dir()
    try:
        names = [p.name for p in root.iterdir()] if root.is_dir() else []
    except OSError:
        return []
    live: list[RunRef] = []
    for name in names:
        m = _REPORT_NAME_RE.match(name)
        if m is None:
            continue
        try:
            live.extend(r for r in _refs(m.group(1), m.group(2)) if r.live)
        except (OSError, ValueError):
            log.warning("workbench: the runs of %s cannot be listed", name)
    floor = datetime.min.replace(tzinfo=UTC)
    live.sort(key=lambda r: (r.built or floor, r.day, r.ticker), reverse=True)
    return [RecentRun(r, _card_tiers(r)) for r in live[:limit]]
