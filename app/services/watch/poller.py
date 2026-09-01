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


def find_filing(submissions: dict, forms: tuple[str, ...], since: date) -> Filing | None:
    """Newest filing of a watched form filed on/after `since`.

    `since` is the print date, which is what makes this safe without tracking
    state between polls: the periodic report for the quarter just announced can
    only be filed on or after the announcement. Exact form matching keeps
    amendments (`10-Q/A`) from re-triggering a case that already ran.
    """
    wanted = {f.upper() for f in forms}
    hits = [
        f for f in recent_filings(submissions)
        if f.form.upper() in wanted and f.filing_date >= since
    ]
    if not hits:
        return None
    return max(hits, key=lambda f: (f.filing_date, f.accepted or ""))


def decide(
    watch: Watch,
    submissions: dict,
    *,
    since: date | None = None,
    now: datetime | None = None,
) -> Decision:
    """Pure decision step: what should the poller do right now?

    Order matters. The filing is checked first so that a name with no thesis is
    only ever flagged once its filing is actually up — before that there is
    nothing to refuse and the correct state is simply "waiting".
    """
    since = since or watch.print_at.date()
    filing = find_filing(submissions, watch.forms, since)
    if filing is None:
        return Decision(
            "wait",
            f"{watch.ticker}: no {'/'.join(watch.forms)} filed on/after {since.isoformat()} yet",
        )

    gate = thesis_state(watch.ticker)
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
