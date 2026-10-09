"""Add a ticker to the watchlist, or remove one, from the workbench.

Adding is `scripts/watch.py add`'s own path, not a second one: the row is
derived from the issuer's filing history by that script's ``_arm`` (print
hint from the 8-K Item 2.02 cadence, event identity from the periodic
filings) and written by `watchlist.add_entry`, which validates before it
writes. Two derivations of one calendar row would drift, and the poller
trusts that row to decide which filing counts. The script is loaded from
its file (scripts/ is not a package); only the SEC fetch around it is here.

Removing refuses a row with a pinned thesis — an event in flight, which
`watch.py sync --prune` keeps for the same reason; that one is removed by
hand, on purpose.

Whatever stops an add or a remove is one line on the page, never a 500
(review of 9d00328): a filing index of an unexpected shape (`PollerError`
from ``_arm``), a watchlist that cannot be written or locked (`OSError`: a
link at its lock is refused, ELOOP).
"""

from __future__ import annotations

import importlib.util
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

from app.services.ingestion.sec_client import SecClient, SecClientError
from app.services.journal import store
from app.services.watch import watchlist as wl
from app.services.watch.poller import PollerError

ROOT = Path(__file__).resolve().parents[3]
WATCH_SCRIPT = ROOT / "scripts" / "watch.py"


class WatchRefused(ValueError):
    """An add or remove that did not happen, with the reason to show."""


@cache
def _watch_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fqe_watch_script", WATCH_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {WATCH_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _submissions(ticker: str) -> dict[str, Any]:
    """The issuer's filing index, as `watch.py add` fetches it."""
    client = SecClient()
    return client.submissions_by_cik(client.resolve_cik(ticker))


def add(ticker: str) -> wl.Watch:
    """Arm ``ticker`` on the watchlist from its filing history; raises
    `WatchRefused` with the reason (SEC down, nothing to infer from,
    already watched)."""
    t = store.safe_ticker(ticker)
    try:
        submissions = _submissions(t)
    except (SecClientError, ValueError) as e:  # ValueError: a payload SEC sent malformed
        raise WatchRefused(f"SEC fetch failed: {e}") from e
    try:
        watch: wl.Watch = _watch_script()._arm(t, submissions)
    except wl.WatchlistError as e:
        raise WatchRefused(str(e)) from e
    except PollerError as e:
        raise WatchRefused(f"SEC's filing index for {t} cannot be read: {e}") from e
    except OSError as e:
        raise WatchRefused(_unchanged(e)) from e
    return watch


def _unchanged(e: OSError) -> str:
    return f"The watchlist could not be changed: {e}"


def remove(ticker: str) -> None:
    """Drop ``ticker``'s row; raises `WatchRefused` when it is not there or
    has a thesis pinned."""
    t = store.safe_ticker(ticker)
    try:
        watch = next((w for w in wl.load() if w.ticker == t), None)
    except wl.WatchlistError as e:
        raise WatchRefused(str(e)) from e
    except OSError as e:
        raise WatchRefused(f"The watchlist cannot be read: {e}") from e
    if watch is None:
        raise WatchRefused(f"{t} is not on the watchlist")
    if watch.thesis_entry:
        raise WatchRefused(f"{t} has a pinned thesis ({watch.thesis_entry}): its event is in "
                           "flight, so it is not removed from here. Remove it by hand from "
                           "journal/watchlist.json once the event is done.")
    try:
        wl.remove_entry(t)
    except wl.WatchlistError as e:
        raise WatchRefused(str(e)) from e
    except OSError as e:
        raise WatchRefused(_unchanged(e)) from e
