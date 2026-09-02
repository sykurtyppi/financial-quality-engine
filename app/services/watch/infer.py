"""Print-date inference from the issuer's own 8-K Item 2.02 cadence.

Aggregator earnings calendars were wrong often enough last season that the
watchlist grew a "verify against IR" warning. The issuer's own filing history
is the one source that cannot be wrong about itself: every past print is an
8-K with Item 2.02 (Results of Operations) whose `acceptanceDateTime` is the
minute the release hit EDGAR. Quarterly cadence + median acceptance clock time
(which naturally separates AMC from BMO filers) predict the next one.

The estimate must be EARLY-biased. The poller's `since` filter drops filings
dated before print_at, so a late estimate does not merely start polling late —
it can exclude the actual filing and wait forever. An early estimate just
polls idle for a few extra sessions. Real cadences wobble ±7d around 91
(NVDA's last six gaps: 90, 91, 84, 98, 83, 98), so the projection uses the
MINIMUM quarterly-band gap observed recently, not the median. Guards reject
rather than guess: fewer than three recent gaps inside the quarterly band
(75-105d, the TTM constructor's bounds) returns None and the caller falls
back to a manual --print-at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from app.services.formulas.ttm import MAX_GAP_DAYS, MIN_GAP_DAYS
from app.services.watch.poller import recent_filings

MIN_IN_BAND_GAPS = 3
GAPS_USED = 6  # most recent inter-print gaps examined


@dataclass(frozen=True)
class PrintEstimate:
    print_at: datetime
    basis: str  # human-readable derivation, stored as the watchlist note


def _acceptance(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def earnings_acceptances(submissions: dict) -> list[datetime]:
    """UTC acceptance times of 8-K Item 2.02 filings, oldest first."""
    hits = []
    for f in recent_filings(submissions):
        if not f.form.upper().startswith("8-K"):
            continue
        if "2.02" not in (f.items or ""):
            continue
        dt = _acceptance(f.accepted)
        if dt is not None:
            hits.append(dt)
    return sorted(hits)


def infer_print_at(submissions: dict, now: datetime | None = None) -> PrintEstimate | None:
    now = now or datetime.now(timezone.utc)
    prints = earnings_acceptances(submissions)
    gaps = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(prints, prints[1:])
    ][-GAPS_USED:]
    in_band = [g for g in gaps if MIN_GAP_DAYS <= g <= MAX_GAP_DAYS]
    if len(in_band) < MIN_IN_BAND_GAPS:
        return None

    gap_days = min(in_band)  # early bias: see module docstring
    last = prints[-1]
    estimate = last + timedelta(days=gap_days)
    if estimate <= now:
        # Between-print add: the next print is one more cadence step out.
        estimate += timedelta(days=gap_days)

    # Clock time from the observed acceptances, not the projected date — the
    # median of recent prints separates ~21:20Z AMC filers from ~11:00Z BMO.
    tod = sorted(p.hour * 3600 + p.minute * 60 + p.second for p in prints[-GAPS_USED:])
    tod_s = int(median(tod))
    estimate = estimate.replace(
        hour=tod_s // 3600, minute=(tod_s % 3600) // 60, second=0, microsecond=0
    )

    basis = (
        f"print_at inferred from {len(prints)} 8-K 2.02 acceptances: "
        f"min in-band gap {gap_days:.0f}d of last {len(gaps)} "
        f"({', '.join(f'{g:.0f}' for g in gaps)}), early-biased; "
        f"last print {last:%Y-%m-%d %H:%M}Z. Verify against company IR."
    )
    return PrintEstimate(print_at=estimate, basis=basis)
