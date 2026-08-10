#!/usr/bin/env python3
"""Decision-impact journal — the tool that decides whether the engine is worth keeping.

The one methodological rule: WRITE YOUR THESIS BEFORE YOU READ THE REPORT.
This CLI enforces that structurally by splitting each case into two steps with
timestamps, so hindsight cannot leak backward.

    # 1. Before reading anything — lock your prior view
    journal.py open NVDA --thesis "beat likely priced in; watching inventory" --conviction 3

    # 2. Generate the report (refused until your thesis is written)
    journal.py report NVDA           # EDGAR_IDENTITY must be set

    # 3. Read the report, fill the AFTER block (impact: one of the four codes)

    # 4. Weeks later, record what actually happened
    journal.py outcome NVDA --date 2026-07-31

    # 5. Any time — see where you stand
    journal.py tally

A browser UI over the identical entry files: `.venv/bin/uvicorn app.web:app`.
Core logic lives in app/services/journal/ so the CLI and the web UI never diverge.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.journal import store
from app.services.journal.reporting import build_report
from app.services.journal.schema_v2 import (
    Assumption,
    BeforeBlock,
    EntryV2,
    lock_entry,
    verify_lock,
)


def cmd_open(args: argparse.Namespace) -> int:
    try:
        path = store.open_entry(args.ticker, args.thesis, args.conviction, args.action)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    except FileExistsError as e:
        print(f"Entry already exists: {e}. Not overwriting.", file=sys.stderr)
        return 1
    print(f"Opened {path}")
    print("Write your BEFORE block now (thesis + conviction), THEN run:")
    print(f"    journal.py report {args.ticker.upper()}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No open entry for {args.ticker.upper()}. Run `journal.py open` first.", file=sys.stderr)
        return 1
    text = path.read_text()
    if not store.has_thesis(text):
        print("BEFORE block looks empty — write your thesis first. Refusing to generate the "
              "report (that is the whole point).", file=sys.stderr)
        return 1
    if store.is_reported(text):
        print("This case's report was already generated; not regenerating "
              "(prevents peeking-then-editing).", file=sys.stderr)
        return 1
    print("Generating report...")
    try:
        entry = store.parse_entry(path)
        out, overall = build_report(args.ticker, with_docs=not args.no_docs, report_day=entry["day"])
    except Exception as e:  # noqa: BLE001
        print(f"Report generation failed: {e}", file=sys.stderr)
        return 1
    store.mark_reported(path)
    print(f"Thesis locked at {store.now_iso()}.")
    print(f"overall: {overall} -> {out}")
    print(f"\nNow read the report and fill the AFTER block in {path}")
    print(f"  impact: one of {', '.join(store.IMPACT_CODES)}")
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry found for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    print(f"Edit the OUTCOME block in {path} (outcome_date, what_happened, verdict).")
    return 0


def _parse_assumption(spec: str) -> Assumption:
    """Parse `metric,comparator,threshold,window,source,resolve_by`. Threshold
    is numeric if it parses, else kept as a symbolic string (e.g. 'positive')."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(
            f"assumption must have 6 comma-separated fields: "
            f"metric,comparator,threshold,window,source,resolve_by (got {len(parts)})"
        )
    metric, cmp_, thr, window, source, resolve_by = parts
    try:
        threshold: float | str = float(thr)
    except ValueError:
        threshold = thr
    from datetime import date as _date
    return Assumption(
        metric=metric,
        comparator=cmp_,  # type: ignore[arg-type]  # Literal validated by pydantic
        threshold=threshold,
        window=window,
        source=source,  # type: ignore[arg-type]
        resolve_by=_date.fromisoformat(resolve_by),
    )


def cmd_openv2(args: argparse.Namespace) -> int:
    """P1-E: open + lock a v2 entry in one step. The anti-annoyance rule is that
    thesis + conviction + one assumption row is all you need."""
    from datetime import date, datetime, timezone

    try:
        ticker = store.safe_ticker(args.ticker)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1

    try:
        assumptions = [_parse_assumption(spec) for spec in (args.assumption or [])]
    except (argparse.ArgumentTypeError, ValueError) as e:
        print(f"Bad --assumption: {e}", file=sys.stderr)
        return 1

    try:
        before = BeforeBlock(
            thesis=args.thesis,
            conviction=args.conviction,
            conviction_fine=args.conviction_fine,
            intended_action=args.action,
            catalyst=args.catalyst,
            contamination=args.contamination,
            p_outcome=args.p_outcome,
            reference_class=args.reference_class,
            assumptions=assumptions,
            falsifiers=args.falsifier or [],
        )
        day = date.today() if not args.date else date.fromisoformat(args.date)
        entry = EntryV2(
            ticker=ticker, day=day,
            opened=datetime.now(timezone.utc),
            before=before,
        )
    except Exception as e:  # pydantic ValidationError or bad --date
        print(f"Could not build entry: {e}", file=sys.stderr)
        return 1

    try:
        locked = lock_entry(entry)
    except ValueError as e:
        print(f"Cannot lock: {e}", file=sys.stderr)
        return 1

    try:
        path = store.save_v2(locked)
    except FileExistsError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    print(f"Locked {path}")
    print(f"before_sha256: {locked.before_sha256}")
    print(f"assumptions: {len(locked.before.assumptions)}"
          + (f", falsifiers: {len(locked.before.falsifiers)}" if locked.before.falsifiers else ""))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Recompute the BEFORE-block hash and report tamper status."""
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    if not store.is_v2(path):
        print(f"{path.name}: v1 entry (no hash lock)")
        return 0
    entry = store.load_v2(path)
    if verify_lock(entry):
        print(f"{path.name}: LOCK INTACT (sha256 {entry.before_sha256[:16]}…)")
        return 0
    print(f"{path.name}: LOCK BROKEN — BEFORE block has been edited since lock", file=sys.stderr)
    return 2


def cmd_tally(args: argparse.Namespace) -> int:
    t = store.tally()
    if not t["total"]:
        print("No journal entries yet. Start with `journal.py open TICKER`.")
        return 0
    print(f"Journal tally — {t['total']} cases logged ({t['scored']} scored, "
          f"{t['with_outcome']} with outcomes)\n")
    print("Impact (of scored cases):")
    for code in store.IMPACT_CODES:
        n = t["impact_counts"][code]
        pct = f"{n / t['scored']:.0%}" if t["scored"] else "—"
        print(f"  {code:20s} {n:3d}  ({pct})")
    if t["scored"]:
        print(f"\n  changed something (not no_value): {t['changed_any']}/{t['scored']} "
              f"({t['changed_any'] / t['scored']:.0%})")
        print(f"  conviction moved: {t['conv_moved']}, unchanged: {t['conv_same']}")
    if t["verdicts"]:
        print("\nOutcomes (where recorded):")
        for v, n in sorted(t["verdicts"].items(), key=lambda kv: -kv[1]):
            print(f"  {v:12s} {n}")
    missing_after = [e["name"] for e in t["entries"] if e["needs_after"]]
    missing_outcome = [e["name"] for e in t["entries"] if e["needs_outcome"]]
    if missing_after:
        print(f"\n{len(missing_after)} cases still need an AFTER block: "
              f"{', '.join(missing_after[:8])}{' ...' if len(missing_after) > 8 else ''}")
    if missing_outcome:
        print(f"{len(missing_outcome)} cases still need an OUTCOME (revisit in a few weeks).")
    if t["gate_ready"]:
        print("\n>=20 cases with outcomes: enough to judge. Decision gate — would you keep "
              "using it voluntarily? See docs/evaluation_protocol.md.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Decision-impact journal for the earnings-quality engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="lock your prior view before reading the report")
    p_open.add_argument("ticker")
    p_open.add_argument("--thesis")
    p_open.add_argument("--conviction", type=int, choices=range(1, 6))
    p_open.add_argument("--action")
    p_open.set_defaults(func=cmd_open)

    p_rep = sub.add_parser("report", help="lock the thesis timestamp and generate the report")
    p_rep.add_argument("ticker")
    p_rep.add_argument("--date")
    p_rep.add_argument("--no-docs", action="store_true")
    p_rep.set_defaults(func=cmd_report)

    p_out = sub.add_parser("outcome", help="record what actually happened, weeks later")
    p_out.add_argument("ticker")
    p_out.add_argument("--date")
    p_out.set_defaults(func=cmd_outcome)

    p_tal = sub.add_parser("tally", help="summarize decision impact across all cases")
    p_tal.set_defaults(func=cmd_tally)

    # ------ P1-E v2: preregistration + hash-lock ------
    p_o2 = sub.add_parser(
        "openv2",
        help="P1-E: lock a v2 (preregistered, hash-locked) entry in one step",
    )
    p_o2.add_argument("ticker")
    p_o2.add_argument("--thesis", required=True)
    p_o2.add_argument("--conviction", type=int, choices=range(1, 6), required=True)
    p_o2.add_argument("--action",
                      choices=["hold", "trim", "add", "avoid", "no_position"], required=True)
    p_o2.add_argument("--assumption", action="append", default=[],
                      help="repeatable: metric,comparator,threshold,window,source,resolve_by")
    p_o2.add_argument("--falsifier", action="append", default=[],
                      help='repeatable: "I am wrong if <observable>"')
    p_o2.add_argument("--catalyst", help="expected event + date")
    p_o2.add_argument("--contamination", help="what analysis PRECEDED this entry")
    p_o2.add_argument("--p-outcome", dest="p_outcome", type=float,
                      help="0.0–1.0; enables Brier scoring")
    p_o2.add_argument("--reference-class", dest="reference_class")
    p_o2.add_argument("--conviction-fine", dest="conviction_fine", type=int,
                      help="optional 0–100 fine grain")
    p_o2.add_argument("--date", help="entry day YYYY-MM-DD (defaults to today)")
    p_o2.set_defaults(func=cmd_openv2)

    p_ver = sub.add_parser("verify", help="recompute the v2 BEFORE-block hash and report tamper status")
    p_ver.add_argument("ticker")
    p_ver.add_argument("--date")
    p_ver.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
