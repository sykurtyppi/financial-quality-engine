"""The earnings calendar the nudge and poller read from.

Deliberately NOT a store of views. It holds only timing — which name reports
when — because the BEFORE thesis is always the analyst's, written blind
(journal/JOURNAL.md rule 1). Keeping the calendar and the priors in separate
files is what lets the calendar be shared/committed while the priors stay
private in the gitignored `journal/entries/`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.services.journal import store

ROOT = Path(__file__).resolve().parents[3]
WATCHLIST = ROOT / "journal" / "watchlist.json"

# The forms that carry the XBRL financials the engine actually needs. An 8-K
# earnings release lands first and is picked up as a document, but it is the
# periodic report that makes the quarter analyzable.
DEFAULT_FORMS = ("10-Q", "10-K")


class WatchlistError(ValueError):
    """Malformed watchlist. Raised with the offending entry named."""


@dataclass(frozen=True)
class Watch:
    """One name on the calendar."""

    ticker: str
    print_at: datetime
    label: str | None = None
    forms: tuple[str, ...] = DEFAULT_FORMS
    note: str | None = None

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

    return Watch(
        ticker=ticker,
        print_at=_parse_dt(raw.get("print_at"), ticker),
        label=(raw.get("label") or None),
        forms=forms,
        note=(raw.get("note") or None),
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


def add_entry(raw: dict, path: Path | None = None) -> Watch:
    """Validate one entry and append it to the watchlist file.

    Validation runs BEFORE the write (parse_watch raises on anything the
    loader would later choke on), so a bad `add` can never corrupt the file a
    cron job reads. Existing content — including the `_comment` block — is
    preserved as-is.
    """
    watch = parse_watch(raw)
    p = path or WATCHLIST
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
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return watch


def due(watches: list[Watch], within_hours: float, now: datetime | None = None) -> list[Watch]:
    """Names whose print is inside the window and still ahead of us.

    Past prints drop out: the nudge exists to catch you BEFORE the print, and a
    reminder to write a blind prior on a quarter already reported is worse than
    no reminder — it invites a thesis written with the tape already visible.
    """
    now = now or datetime.now(timezone.utc)
    return [w for w in watches if 0 < w.hours_until(now) <= within_hours]
