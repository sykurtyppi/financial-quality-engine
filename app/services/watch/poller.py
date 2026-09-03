"""Filing-landed detection and the thesis gate.

The gate is the reason this module exists. Automating report generation is
trivial; automating it *safely* is not. journal/JOURNAL.md rule 1 is "thesis
before report, always", and the value of every entry depends on the BEFORE
block having been written blind. A scheduler that generates reports on the
calendar would leave a readable report sitting in `reports/` before the analyst
has written a prior — after which no blind case for that name is possible.

So the automation is inverted: the filing landing does NOT authorize a report.
A locked thesis does. `decide()` is pure so the refusal paths are covered by
offline tests rather than trusted to a live earnings night.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from app.services.journal import store
from app.services.journal.schema_v2 import verify_lock
from app.services.watch.watchlist import Watch


class PollerError(RuntimeError):
    pass


class Gate(str, Enum):
    """Why the poller may or may not generate a report for this name."""

    NO_ENTRY = "no_entry"
    NO_PINNED = "no_pinned_thesis"
    PLACEHOLDER_THESIS = "placeholder_thesis"
    LOCK_BROKEN = "lock_broken"
    ALREADY_REPORTED = "already_reported"
    LOCKED = "locked"


@dataclass(frozen=True)
class GateResult:
    state: Gate
    detail: str
    path: Path | None = None

    @property
    def may_generate(self) -> bool:
        return self.state is Gate.LOCKED


@dataclass(frozen=True)
class Filing:
    form: str
    accession: str
    filing_date: date
    report_date: date | None = None
    accepted: str | None = None
    primary_document: str | None = None
    items: str | None = None  # 8-K item codes, e.g. "2.02,9.01"


@dataclass(frozen=True)
class Decision:
    action: str  # generate | wait | refuse | skip
    message: str
    gate: GateResult | None = None
    filing: Filing | None = None


def thesis_state(ticker: str, day: str | None = None) -> GateResult:
    """Classify the journal entry backing `ticker`. The only state that
    authorizes generation is LOCKED: an entry exists, carries a real thesis,
    and has not been reported yet."""
    path = store.find_entry(ticker, day)
    if path is None:
        return GateResult(
            Gate.NO_ENTRY,
            f"no journal entry for {ticker} — open one with a thesis before the print",
        )

    if store.is_v2(path):
        entry = store.load_v2(path)
        if not verify_lock(entry):
            return GateResult(
                Gate.LOCK_BROKEN,
                f"{path.name}: BEFORE-block hash does not match — refusing to act on a "
                f"tampered entry (run `journal.py verify {ticker}`)",
                path,
            )
        if entry.reported is not None:
            return GateResult(Gate.ALREADY_REPORTED, f"{path.name}: already reported", path)
        # A v2 BEFORE block cannot exist without a thesis (schema min_length=1).
        return GateResult(Gate.LOCKED, f"{path.name}: thesis locked", path)

    text = path.read_text()
    if store.is_reported(text):
        return GateResult(Gate.ALREADY_REPORTED, f"{path.name}: already reported", path)
    if not store.has_thesis(text):
        return GateResult(
            Gate.PLACEHOLDER_THESIS,
            f"{path.name}: BEFORE block still holds the placeholder — write the thesis",
            path,
        )
    return GateResult(Gate.LOCKED, f"{path.name}: thesis locked", path)


def _as_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def recent_filings(submissions: dict) -> list[Filing]:
    """Flatten the parallel-array `filings.recent` block into records."""
    try:
        recent = submissions["filings"]["recent"]
        forms = recent["form"]
        accessions = recent["accessionNumber"]
        filed = recent["filingDate"]
    except (KeyError, TypeError) as e:
        raise PollerError(f"unexpected submissions payload shape: missing {e}") from e

    report_dates = recent.get("reportDate") or [None] * len(forms)
    accepted = recent.get("acceptanceDateTime") or [None] * len(forms)
    primary = recent.get("primaryDocument") or [None] * len(forms)
    items = recent.get("items") or [None] * len(forms)

    out: list[Filing] = []
    for i, form in enumerate(forms):
        fd = _as_date(filed[i])
        if fd is None:
            continue
        out.append(
            Filing(
                form=form,
                accession=accessions[i],
                filing_date=fd,
                report_date=_as_date(report_dates[i] if i < len(report_dates) else None),
                accepted=accepted[i] if i < len(accepted) else None,
                primary_document=primary[i] if i < len(primary) else None,
                items=items[i] if i < len(items) else None,
            )
        )
    return out


# A fiscal quarter end can drift a couple of weeks around the expected date
# (52/53-week calendars, 4-4-5 retailers); an ADJACENT quarter is ~91 days
# away, so a 21-day tolerance separates "this event, slightly shifted" from
# "a different quarter entirely".
REPORT_DATE_TOL_DAYS = 21


def _qualifies(f: Filing, wanted: set[str]) -> bool:
    """Form match, with 8-K narrowed to Item 2.02 (earnings). An 8-K without
    2.02 — a debt agreement, an officer departure — is not the print."""
    if f.form.upper() not in wanted:
        return False
    if f.form.upper().startswith("8-K") and "2.02" not in (f.items or ""):
        return False
    return True


def _newest(hits: list[Filing]) -> Filing:
    # Deterministic: acceptance timestamp, then filing date, then accession —
    # never dict/iteration order.
    return max(hits, key=lambda f: (f.filing_date, f.accepted or "", f.accession))


def find_filing(
    submissions: dict,
    forms: tuple[str, ...],
    *,
    baseline_accession: str,
    expected_report_date: date,
    tolerance_days: int = REPORT_DATE_TOL_DAYS,
) -> Filing | None:
    """The event-identified trigger: a filing counts iff it is a NEW accession
    (not the baseline recorded when the watch was armed) whose report period
    matches the expected fiscal period.

    `print_at` is deliberately absent here. A forecast date must never act as
    a filing cutoff — a company reporting earlier than estimated would have
    its real filing excluded on every subsequent poll, forever. The baseline
    accession + expected period identify the filing regardless of WHEN it
    arrives; the estimate only schedules when polling starts.
    """
    wanted = {f.upper() for f in forms}
    hits = []
    for f in recent_filings(submissions):
        if not _qualifies(f, wanted):
            continue
        if f.accession == baseline_accession:
            continue
        if f.form.upper().startswith("8-K"):
            # An earnings 8-K's reportDate is the event date, not the quarter
            # end — for the intended quarter it can only come after that
            # quarter ends, and before the NEXT quarter's release cycle
            # (one ~91d period + tolerance); a later quarter's 2.02 must not
            # win the newest-selection on a long-forgotten watch.
            age = (f.filing_date - expected_report_date).days
            if age < 0 or age > 91 + tolerance_days:
                continue
        else:
            if f.report_date is None:
                continue
            if abs((f.report_date - expected_report_date).days) > tolerance_days:
                continue  # an intervening earlier (or later) quarter is NOT this event
        hits.append(f)
    return _newest(hits) if hits else None


def find_filing_since(submissions: dict, forms: tuple[str, ...], since: date) -> Filing | None:
    """Ad-hoc variant for an OPERATOR-SUPPLIED lower bound (`poll --since`).
    Never called with a forecast date — that was the defect: an estimate used
    as a cutoff silently excludes a filing that arrives early."""
    wanted = {f.upper() for f in forms}
    hits = [
        f for f in recent_filings(submissions)
        if _qualifies(f, wanted) and f.filing_date >= since
    ]
    return _newest(hits) if hits else None


def pinned_thesis_state(watch: Watch) -> GateResult:
    """The event-pinned gate: only the journal entry the watch was linked to —
    verified against the lock hash recorded at link time — can authorize this
    event's report.

    Never falls back to "latest entry for the ticker": that fallback is how a
    May thesis got stamped as evidence for an August quarter. No pin, no
    journal-track generation.
    """
    if not watch.thesis_entry or not watch.thesis_sha256:
        return GateResult(
            Gate.NO_PINNED,
            f"no thesis pinned to this event — open a v2 entry "
            f"(`journal.py openv2 {watch.ticker} ...`) then `watch.py link {watch.ticker}`",
        )
    path = store.find_entry(watch.ticker, watch.thesis_entry)
    if path is None:
        return GateResult(
            Gate.NO_ENTRY,
            f"pinned entry {watch.ticker}_{watch.thesis_entry}.md is missing",
        )
    if not store.is_v2(path):
        return GateResult(
            Gate.LOCK_BROKEN,
            f"{path.name}: pinned entry is not a hash-locked v2 entry — the pin "
            f"cannot be verified. Re-open with `journal.py openv2` and re-link.",
            path,
        )
    entry = store.load_v2(path)
    if not verify_lock(entry):
        return GateResult(
            Gate.LOCK_BROKEN,
            f"{path.name}: BEFORE-block hash does not match — refusing to act on a "
            f"tampered entry (run `journal.py verify {watch.ticker}`)",
            path,
        )
    if entry.before_sha256 != watch.thesis_sha256:
        return GateResult(
            Gate.LOCK_BROKEN,
            f"{path.name}: entry lock hash differs from the hash pinned on the watch "
            f"— the entry on disk is not the one linked to this event",
            path,
        )
    if entry.reported is not None:
        return GateResult(Gate.ALREADY_REPORTED, f"{path.name}: already reported", path)
    return GateResult(Gate.LOCKED, f"{path.name}: thesis locked (pinned)", path)


def decide(
    watch: Watch,
    submissions: dict,
    *,
    since: date | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> Decision:
    """Pure decision step: what should the poller do right now?

    Order matters. The filing is checked first so that a name with no thesis is
    only ever flagged once its filing is actually up — before that there is
    nothing to refuse and the correct state is simply "waiting".

    `since` is ONLY for an operator-supplied ad-hoc lower bound. Without it the
    watch must carry event identity (baseline accession + expected period);
    a legacy row without those fields fails closed rather than guessing from
    the forecast print date. When the watch DOES carry event identity, a
    `since`-matched filing must be the same one the event identity selects —
    a stale `--since` on an armed watch would otherwise trigger on an old
    filing and permanently consume the pinned entry (ALREADY_REPORTED is
    terminal). `force=True` overrides the cross-check.
    """
    if since is not None:
        filing = find_filing_since(submissions, watch.forms, since)
        if filing is not None and watch.event_armed and not force:
            event_match = find_filing(
                submissions,
                watch.forms,
                baseline_accession=watch.baseline_accession,
                expected_report_date=watch.expected_report_date,
            )
            if event_match is None or event_match.accession != filing.accession:
                raise PollerError(
                    f"{watch.ticker}: --since matched {filing.form} {filing.accession} "
                    f"(filed {filing.filing_date.isoformat()}), but the armed watch "
                    f"expects period ~{watch.expected_report_date.isoformat()} beyond "
                    f"baseline {watch.baseline_accession or '(none)'}"
                    + (f" — which selects {event_match.form} {event_match.accession} "
                       f"instead" if event_match else " — which matches nothing yet")
                    + ". A mismatched trigger would consume the pinned entry for the "
                    "wrong event. Drop --since, or pass --force to override."
                )
    else:
        if not watch.event_armed:
            raise PollerError(
                f"{watch.ticker}: watch has no event identity "
                f"(baseline_accession + expected_report_date). Re-arm it with "
                f"`watch.py add {watch.ticker}` (or set the fields) — polling on "
                f"a forecast date can miss an early filing forever."
            )
        filing = find_filing(
            submissions,
            watch.forms,
            baseline_accession=watch.baseline_accession,
            expected_report_date=watch.expected_report_date,
        )
    if filing is None:
        what = (
            f"on/after {since.isoformat()}" if since is not None
            else f"for period ~{watch.expected_report_date.isoformat()} "
                 f"beyond baseline {watch.baseline_accession or '(none)'}"
        )
        return Decision(
            "wait",
            f"{watch.ticker}: no qualifying {'/'.join(watch.forms)} {what} yet",
        )

    gate = pinned_thesis_state(watch)
    landed = f"{filing.form} {filing.accession} filed {filing.filing_date.isoformat()}"

    if gate.state is Gate.ALREADY_REPORTED:
        return Decision("skip", f"{watch.ticker}: {landed}; {gate.detail}", gate, filing)
    if gate.may_generate:
        return Decision("generate", f"{watch.ticker}: {landed}; {gate.detail}", gate, filing)
    return Decision(
        "refuse",
        f"{watch.ticker}: {landed} — but {gate.detail}. NOT generating: a report read "
        f"before a thesis is written cannot become a blind case.",
        gate,
        filing,
    )
