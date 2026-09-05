"""Event identity and re-arming: what the watch should look for NEXT.

A watch's event identity (baseline accession + expected report period) is
deliberately one-shot — it names exactly one filing, so nothing else can
trigger it. That is also why a watch cannot survive its own print unattended:
once the filing lands, the identity is spent and someone had to `add` the
name again. Re-arming derives the next identity from the issuer's own filing
history the moment the current one is consumed, so a name on the calendar
stays on it across the season with no operator step per print.

Pure functions over the submissions payload; the CLI does the persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from app.services.formulas.ttm import MAX_GAP_DAYS, MIN_GAP_DAYS
from app.services.watch.infer import infer_print_at
from app.services.watch.poller import Filing, recent_filings

DEFAULT_PERIOD_STEP_DAYS = 91
# Early-biased fallback when the 8-K 2.02 cadence cannot be inferred: the
# next print is ~a quarter after the filing that just landed, and an early
# hint only costs idle polls (an accession never triggers on the date).
FALLBACK_PRINT_LAG_DAYS = 84
# A print hint this close to the consumed filing is the print that just
# happened, not the next one (the 10-Q can trail the 8-K by up to ~a week+).
CONSUMED_PRINT_WINDOW_DAYS = 45


def event_identity(submissions: dict, forms: tuple[str, ...]) -> tuple[str, date | None]:
    """(baseline_accession, expected_report_date) from the filing history.

    Baseline = newest qualifying accession right now, so only a LATER accession
    can trigger. Expected period = newest periodic report period + the median
    in-band period gap (~91d) — what distinguishes THIS event's filing from an
    intervening earlier quarter's.
    """
    wanted = {f.upper() for f in forms}
    filings = recent_filings(submissions)
    periodic = [
        f for f in filings
        if f.form.upper() in {"10-Q", "10-K"} and f.report_date is not None
    ]
    qualifying = [
        f for f in filings
        if f.form.upper() in wanted
        and (not f.form.upper().startswith("8-K") or "2.02" in (f.items or ""))
    ]
    baseline = ""
    if qualifying:
        newest = max(qualifying, key=lambda f: (f.filing_date, f.accepted or "", f.accession))
        baseline = newest.accession
    if not periodic:
        return baseline, None
    periods = sorted({f.report_date for f in periodic})
    gaps = [(b - a).days for a, b in zip(periods, periods[1:])]
    in_band = [g for g in gaps[-6:] if MIN_GAP_DAYS <= g <= MAX_GAP_DAYS]
    step = int(median(in_band)) if in_band else DEFAULT_PERIOD_STEP_DAYS
    return baseline, periods[-1] + timedelta(days=step)


@dataclass(frozen=True)
class Arming:
    baseline_accession: str
    expected_report_date: date
    print_at: datetime
    note: str

    def as_updates(self) -> dict:
        """Watchlist-row updates. The pin and label belong to the event just
        consumed — a spent entry must never authorize the next quarter, and
        ``None`` values are dropped from the row by ``update_entry``."""
        return {
            "baseline_accession": self.baseline_accession,
            "expected_report_date": self.expected_report_date.isoformat(),
            "print_at": self.print_at.isoformat(),
            "note": self.note,
            "thesis_entry": None,
            "thesis_sha256": None,
            "label": None,
        }


def next_arming(
    submissions: dict,
    forms: tuple[str, ...],
    *,
    filed: Filing | None,
    previous_expected: date | None,
    now: datetime | None = None,
) -> Arming:
    """The identity for the NEXT event, derived after ``filed`` consumed the
    current one. Never fails: every field has a stated fallback, because a
    watch left un-re-armed on the auto track would regenerate the same
    report on every sweep.
    """
    now = now or datetime.now(timezone.utc)
    baseline, expected = event_identity(submissions, forms)
    known = {f.accession for f in recent_filings(submissions)}
    if filed is not None and filed.accession not in known:
        # Stale payload (the landed filing is not in it yet): the history's
        # newest accession is OLDER than the one just consumed. Baseline on
        # the consumed filing so it can never re-trigger.
        baseline = filed.accession
    if expected is None:
        anchor = previous_expected or (filed.filing_date if filed else now.date())
        expected = anchor + timedelta(days=DEFAULT_PERIOD_STEP_DAYS)
    elif previous_expected is not None and expected <= previous_expected:
        # The history's newest period is still the one just reported (or
        # older, when companyfacts-style lag hides it): step past it.
        expected = previous_expected + timedelta(days=DEFAULT_PERIOD_STEP_DAYS)

    anchor = filed.filing_date if filed else now.date()
    est = infer_print_at(submissions, now=now)
    if est is not None:
        print_at, basis = est.print_at, est.basis
        # A stale payload (this print's 8-K not in it yet) projects the print
        # that just happened; a hint must point at the NEXT one.
        while print_at.date() <= anchor + timedelta(days=CONSUMED_PRINT_WINDOW_DAYS):
            print_at += timedelta(days=FALLBACK_PRINT_LAG_DAYS)
    else:
        print_at = datetime.combine(
            anchor + timedelta(days=FALLBACK_PRINT_LAG_DAYS), time(12, 0), tzinfo=timezone.utc
        )
        basis = (f"8-K 2.02 cadence not inferable; print_at = filing date + "
                 f"{FALLBACK_PRINT_LAG_DAYS}d (early-biased hint only)")
    consumed = (f"{filed.form} {filed.accession} (filed {filed.filing_date.isoformat()})"
                if filed else "the previous event")
    note = f"re-armed {now:%Y-%m-%d} after {consumed}. {basis}"
    return Arming(
        baseline_accession=baseline,
        expected_report_date=expected,
        print_at=print_at,
        note=note,
    )
