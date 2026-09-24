#!/usr/bin/env python3
"""Companyfacts vintage store: capture snapshots, and diff what moved.

    scripts/vintage.py capture NVDA                 # one name
    scripts/vintage.py capture --portfolio          # every holding
    scripts/vintage.py list NVDA
    scripts/vintage.py diff NVDA                    # newest two snapshots
    scripts/vintage.py diff NVDA --from 2026-09-19 --to 2026-12-01

A company can revise a prior figure without re-presenting the original, and
then companyfacts holds only the new value: the change is invisible from any
single fetch. Only a snapshot taken BEFORE the revision can show it, which is
why the sweep captures from the day this lands. Nothing here can be
back-filled — an uncaptured quarter is gone.

Exit codes: 0 done (a diff with changes also exits 0 — a revision is a
finding, not an error), 1 setup or EDGAR failure, 2 nothing to compare.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.sec_client import SecClient, SecClientError  # noqa: E402
from app.services.ingestion.vintages import (  # noqa: E402
    UNREADABLE,
    capture,
    diff_scored,
    diff_vintages,
    list_vintages,
    load_vintage,
    observed_vintages,
    read_manifest,
    render_changes,
    snapshot_day,
)
from app.services.journal.store import safe_ticker  # noqa: E402
from app.services.watch.watchlist import WatchlistError, read_portfolio  # noqa: E402

PORTFOLIO = ROOT / "journal" / "portfolio.txt"


def cmd_capture(args: argparse.Namespace) -> int:
    try:
        tickers = (read_portfolio(Path(args.portfolio)) if args.portfolio is not None
                   else list(args.tickers))
    except WatchlistError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not tickers:
        print("no tickers given (pass them, or --portfolio)", file=sys.stderr)
        return 1
    try:
        client = SecClient(fresh=True)
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    worst = 0
    for ticker in tickers:
        try:
            t = safe_ticker(ticker)
            res = capture(client, t, force=args.force)
        except Exception as e:  # noqa: BLE001 — one bad name must not end the batch
            print(f"{ticker}: {type(e).__name__}: {e}", file=sys.stderr)
            worst = 1
            continue
        where = f" -> {res.path.name} ({res.path.stat().st_size / 1024:.0f} KB)" if res.wrote else ""
        print(f"{t}: {res.reason}{where}")
    return worst


def cmd_list(args: argparse.Namespace) -> int:
    client = SecClient()
    cik = client.resolve_cik(safe_ticker(args.ticker))
    paths = list_vintages(cik)
    man = read_manifest(cik)
    if not paths:
        print(f"{args.ticker}: no snapshots yet (last checked {man.get('last_checked') or 'never'})")
        return 0
    print(f"{args.ticker} (CIK {cik:010d}) — {len(paths)} snapshot(s), "
          f"last checked {man.get('last_checked') or 'never'}")
    for p in paths:
        print(f"  {snapshot_day(p)}  {p.stat().st_size / 1024:8.0f} KB  {p.name}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    client = SecClient()
    cik = client.resolve_cik(safe_ticker(args.ticker))
    states = observed_vintages(cik)
    if len(states) < 2:
        print(f"{args.ticker}: {len(states)} distinct snapshot state(s) — a diff needs two. "
              "The store fills as the sweep runs; nothing can be back-filled.",
              file=sys.stderr)
        return 2
    by_day = {state.captured: state for state in states}  # last observation that day wins
    older = by_day.get(args.from_day) if args.from_day else states[-2]
    newer = by_day.get(args.to_day) if args.to_day else states[-1]
    if older is None or newer is None:
        print(f"{args.ticker}: no snapshot for "
              f"{args.from_day if older is None else args.to_day}; have "
              f"{', '.join(sorted(by_day))}", file=sys.stderr)
        return 2
    try:
        old_facts, new_facts = load_vintage(older.path), load_vintage(newer.path)
    except UNREADABLE as e:
        print(f"{args.ticker}: a snapshot could not be read ({e}). The file is kept — "
              "never delete it; capture again to add a readable one.", file=sys.stderr)
        return 1
    since = date.fromisoformat(args.since) if args.since else None
    if args.splits:
        # Share counts on request: the raw fact diff, which alone can
        # include split-adjusted fields.
        changes = diff_vintages(old_facts, new_facts, scored_only=True, since=since,
                                include_split_adjusted=True)
    else:
        # What the engine scores, compared as the mapper builds it; raw facts
        # as provenance and pre-window context.
        scored = diff_scored(old_facts, new_facts, since=since)
        changes = scored.changes
        if scored.canonical_unavailable:
            print(f"Note: {scored.canonical_unavailable}.", file=sys.stderr)
    print(render_changes(changes, older.captured, newer.captured))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="snapshot companyfacts if it changed")
    c.add_argument("tickers", nargs="*")
    c.add_argument("--portfolio", nargs="?", const=str(PORTFOLIO), default=None,
                   help=f"capture every holding in this file (default {PORTFOLIO.name})")
    c.add_argument("--force", action="store_true",
                   help="fetch even if already checked today")
    c.set_defaults(fn=cmd_capture)

    ls = sub.add_parser("list", help="snapshots on file for a ticker")
    ls.add_argument("ticker")
    ls.set_defaults(fn=cmd_list)

    d = sub.add_parser("diff", help="what moved between two snapshots")
    d.add_argument("ticker")
    d.add_argument("--from", dest="from_day", help="YYYY-MM-DD (default: second newest)")
    d.add_argument("--to", dest="to_day", help="YYYY-MM-DD (default: newest)")
    d.add_argument("--since", help="ignore periods ending before YYYY-MM-DD")
    d.add_argument("--splits", action="store_true",
                   help="include share counts (a stock split rewrites every prior "
                        "count; excluded by default as a corporate action, not a revision)")
    d.set_defaults(fn=cmd_diff)

    args = p.parse_args()
    try:
        return args.fn(args)
    except (SecClientError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
