#!/usr/bin/env python3
"""Record the one market observation the valuation shadow card reads.

    scripts/market.py record NVDA --price 182.40 --at 2026-11-18T21:00:00+00:00 \\
        --source "NYSE official close (broker statement)"
    scripts/market.py record NVDA --price 182.40 --at 2026-11-18T21:00:00+00:00 \\
        --source "..." --required-return 0.09 --terminal-growth 0.025 --horizon-years 10 \\
        --scenario bull:0.15:5 --scenario bear:-0.05:3:0.0:0.12
    scripts/market.py show NVDA        # the file and the observation's age
    scripts/market.py remove NVDA

One file per ticker, ``journal/market/<TICKER>.json``, replaced whole on
each ``record`` (there is no price history: the card reads one
observation). The price is what YOU looked at, with its exact timestamp
(an offset is required: a naive time is refused) and the source named in
``--source``; nothing is fetched. A scenario is ``name:fcf_growth:years``
with optional ``:terminal_growth:required_return``. The next report of the
ticker (``generate_report.py``, ``journal.py report``, the watch) appends
the shadow card to its appendix; ``--no-market`` on ``generate_report.py``
leaves it out.

Exit codes: 0 ok · 2 invalid input (a bad price, time, currency, scenario
or ticker; a file that is not an observation; nothing to show or remove) ·
1 the file could not be written or removed (I/O; a symlink in the way).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.valuation.observation import (
    STALE_AFTER_DAYS,
    Assumptions,
    MarketObservation,
    ObservationError,
    Scenario,
    find_observation,
    remove_observation,
    write_observation,
)

EXIT_INVALID = 2
EXIT_IO = 1


def _journal() -> Path:
    return ROOT / "journal"


def parse_scenario(text: str) -> Scenario:
    """``name:fcf_growth:years[:terminal_growth[:required_return]]``."""
    parts = text.split(":")
    if not 3 <= len(parts) <= 5:
        raise ValueError(f"scenario {text!r}: expected name:fcf_growth:years"
                         "[:terminal_growth[:required_return]]")
    name, growth, years, *rest = parts
    try:
        return Scenario(
            name=name, fcf_growth=float(growth), years=int(years),
            terminal_growth=float(rest[0]) if rest else None,
            required_return=float(rest[1]) if len(rest) > 1 else None,
        )
    except (ValueError, ValidationError) as e:
        raise ValueError(f"scenario {text!r}: {e}") from None


def parse_at(text: str) -> datetime:
    """An ISO-8601 time with an offset; naive is refused, not assumed UTC."""
    try:
        at = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"--at {text!r}: {e}") from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"--at {text!r} has no UTC offset: write the time as observed, with "
                         "its offset (e.g. 2026-11-18T16:00:00-05:00)")
    return at


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record (replace) the ticker's observation")
    rec.add_argument("ticker")
    rec.add_argument("--price", required=True, type=float, help="the share price observed")
    rec.add_argument("--at", required=True, metavar="ISO8601",
                     help="when it was observed, with its UTC offset")
    rec.add_argument("--source", required=True,
                     help='what you looked at, e.g. "NYSE official close (broker statement)"')
    rec.add_argument("--currency", default="USD", help="three upper-case letters (default USD)")
    rec.add_argument("--note", help="free text (≤500 characters)")
    rec.add_argument("--required-return", type=float, metavar="R",
                     help="the expectations block's discount rate (default 0.09 when none "
                          "of the three is given)")
    rec.add_argument("--terminal-growth", type=float, metavar="G", help="default 0.025")
    rec.add_argument("--horizon-years", type=int, metavar="N", help="default 10")
    rec.add_argument("--scenario", action="append", default=[], metavar="NAME:G:YEARS[:TG[:R]]",
                     help="a named FCF path to value per share (repeatable)")
    show = sub.add_parser("show", help="print the ticker's observation and its age")
    show.add_argument("ticker")
    rm = sub.add_parser("remove", help="remove the ticker's observation")
    rm.add_argument("ticker")
    return parser


def cmd_record(args: argparse.Namespace) -> int:
    try:
        at = parse_at(args.at)
        assumptions = None
        given = {k: v for k, v in (("required_return", args.required_return),
                                   ("terminal_growth", args.terminal_growth),
                                   ("horizon_years", args.horizon_years)) if v is not None}
        if given:
            assumptions = Assumptions(**given)
        obs = MarketObservation(
            ticker=args.ticker, price=args.price, currency=args.currency, observed_at=at,
            source=args.source, note=args.note, recorded_at=datetime.now(UTC),
            assumptions=assumptions, scenarios=tuple(parse_scenario(s) for s in args.scenario),
        )
    except (ValueError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
    try:
        path = write_observation(_journal(), obs)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_IO
    print(f"recorded {obs.ticker} {obs.price:,.2f} {obs.currency} observed "
          f"{obs.observed_at.isoformat()} -> {path}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        loaded = find_observation(_journal(), args.ticker)
    except (ValueError, ObservationError) as e:  # ValueError: the ticker itself
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
    if loaded is None:
        print(f"no market observation recorded for {args.ticker.upper()}", file=sys.stderr)
        return EXIT_INVALID
    obs = loaded.observation
    today = datetime.now(UTC).date()
    age = obs.age_days(today)
    assert loaded.path is not None
    print(loaded.path.read_text(encoding="utf-8").rstrip())
    print(f"age: {age} day{'s' if age != 1 else ''} as of {today} (sha256 {loaded.sha256[:12]}…)"
          + (f" — STALE: older than {STALE_AFTER_DAYS} days" if obs.is_stale(today) else ""))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    try:
        path = remove_observation(_journal(), args.ticker)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_IO
    if path is None:
        print(f"no market observation recorded for {args.ticker.upper()}", file=sys.stderr)
        return EXIT_INVALID
    print(f"removed {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"record": cmd_record, "show": cmd_show, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
