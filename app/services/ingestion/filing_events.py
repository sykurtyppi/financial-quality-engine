"""Dated filing events: what a filer did, and when, from its filing index.

The restatement check reads the numbers; this stream reads the filer's
behaviour. Each event is a filing with a date and an accession:

- 8-K Item 4.02, non-reliance on previously issued statements;
- 8-K Item 4.01, a change of auditor (item code only — the letter is not read);
- 8-K Item 2.06, a material impairment;
- NT 10-K / NT 10-Q, a notice that a periodic report will be late;
- 10-K/A and 10-Q/A, amendments to a periodic report;
- filing-lag drift: an original 10-Q or 10-K filed at least
  `LAG_DRIFT_DAYS` later after its period end than the filer's median over
  its previous filings of that form. A filer that suddenly takes longer to
  close its books is doing something it did not use to need to do.

Only filings made inside the window `[since, as_of]` are events, so a report
dated in the past (a replay) never lists a later filing. The index is the
submissions API's `recent` block, which reaches back ~1000 filings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any

from app.services.ingestion.payloads import recent_filings, sec_date

LAG_DRIFT_DAYS = 7  # hand-set; a week later than the filer's own habit
LAG_HISTORY = 8  # prior filings of the same form the median is taken over
LAG_MIN_HISTORY = 4  # fewer than this and there is no habit to drift from

# Signal names the decision card tiers on (decision_card.TIER1_SIGNALS).
NON_RELIANCE = "non_reliance_8k_402"
AUDITOR_CHANGE = "auditor_change_8k_401"
LATE_FILING = "missed_deadline_nt"


@dataclass(frozen=True)
class FilingEvent:
    kind: str  # non_reliance | auditor_change | impairment | late_filing_notice | amendment | filing_lag
    form: str
    filed: date
    accession: str
    detail: str
    signal: str | None = None  # the card's signal name, when the event has one


@dataclass(frozen=True)
class FilingEvents:
    since: date
    as_of: date
    events: tuple[FilingEvent, ...]
    lag_checked: bool  # False when the index carries no period dates

    def of(self, *signals: str) -> list[FilingEvent]:
        return [e for e in self.events if e.signal in signals]


_ITEMS: tuple[tuple[str, str, str, str | None], ...] = (
    ("4.02", "non_reliance", "8-K Item 4.02 non-reliance (restatement announced)", NON_RELIANCE),
    ("4.01", "auditor_change", "8-K Item 4.01 change of auditor", AUDITOR_CHANGE),
    ("2.06", "impairment", "8-K Item 2.06 material impairment", None),
)


def _items(raw: str | None) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def filing_events(submissions: dict[str, Any], *, since: date, as_of: date) -> FilingEvents:
    """The dated events filed in `[since, as_of]`."""
    # One aligned read: the period dates are an optional column, but when
    # present they must line up with every other column (never zipped).
    rows = recent_filings(
        submissions, form=str, items=(str, type(None)), filingDate=str, accessionNumber=str,
        optional={"reportDate": (str, type(None))},
    )
    recent = submissions.get("filings", {}).get("recent", {})
    lag_checked = isinstance(recent, dict) and "reportDate" in recent

    events: list[FilingEvent] = []
    periodic: dict[str, list[tuple[date, int, str]]] = {"10-Q": [], "10-K": []}
    for form, items, filed_raw, accession, period in rows:
        filed = sec_date(filed_raw, f"{form} filingDate")
        if filed > as_of:
            continue  # not yet filed on the report date: invisible, lag history included
        inside = since <= filed
        if form.startswith("8-K"):
            codes = _items(items)
            for code, kind, text, signal in _ITEMS:
                if code in codes and inside:
                    events.append(FilingEvent(kind, form, filed, accession,
                                              f"{text} filed {filed}", signal))
        elif form.startswith("NT 10-"):
            if inside:
                events.append(FilingEvent("late_filing_notice", form, filed, accession,
                                          f"{form} (notice of late filing) filed {filed}",
                                          LATE_FILING))
        elif form in ("10-K/A", "10-Q/A"):
            if inside:
                events.append(FilingEvent("amendment", form, filed, accession,
                                          f"{form} amendment filed {filed}"))
        elif form in periodic and lag_checked:
            if isinstance(period, str) and period:
                lag = (filed - sec_date(period, f"{form} reportDate")).days
                periodic[form].append((filed, lag, accession))

    for form, history in periodic.items():
        history.sort()
        for n, (filed, lag, accession) in enumerate(history):
            prior = [h[1] for h in history[max(0, n - LAG_HISTORY):n]]
            if filed < since or len(prior) < LAG_MIN_HISTORY:
                continue
            usual = median(prior)
            if lag - usual >= LAG_DRIFT_DAYS:
                events.append(FilingEvent(
                    "filing_lag", form, filed, accession,
                    f"{form} filed {lag} days after its period end, against a median of "
                    f"{usual:g} over the previous {len(prior)} (filed {filed})",
                ))
    events.sort(key=lambda e: (e.filed, e.kind, e.accession), reverse=True)
    return FilingEvents(since, as_of, tuple(events), lag_checked)


_KIND_LABELS = {
    "non_reliance": "Non-reliance (8-K 4.02)",
    "auditor_change": "Auditor change (8-K 4.01)",
    "impairment": "Material impairment (8-K 2.06)",
    "late_filing_notice": "Late-filing notice",
    "amendment": "Amendment",
    "filing_lag": "Filed later than usual",
}


def render_filing_events_section(fe: FilingEvents) -> str:
    """Markdown for the report appendix. Evidence, not scored."""
    lines = ["## Filing Behavior (dated events — not scored)", ""]
    if not fe.events:
        lines.append(
            f"No non-reliance, auditor-change, impairment, late-filing or amendment filings, "
            f"and no filing later than the filer's usual lag, between {fe.since} and {fe.as_of}."
        )
    else:
        lines += [
            f"_Filings between {fe.since} and {fe.as_of} that say something about the filer's "
            "reporting, newest first. Read the filing before drawing a conclusion._",
            "",
            "| Filed | Form | Event | Accession |",
            "|---|---|---|---|",
        ]
        for e in fe.events:
            label = _KIND_LABELS[e.kind]
            if e.kind == "filing_lag":
                label += f": {e.detail.split(' (filed')[0]}"
            lines.append(f"| {e.filed} | {e.form} | {label} | {e.accession} |")
    if not fe.lag_checked:
        lines += ["", "_Filing-lag drift not checked: the filing index carries no period dates._"]
    return "\n".join(lines)
