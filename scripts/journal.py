#!/usr/bin/env python3
"""Decision-impact journal — the tool that decides whether the engine is worth keeping.

The one methodological rule: WRITE YOUR THESIS BEFORE YOU READ THE REPORT.
This CLI enforces that structurally by splitting each case into two steps with
timestamps, so hindsight cannot leak backward.

Daily loop during earnings season:

    # 1. Before market / before reading anything — lock your prior view
    journal.py open NVDA --thesis "beat likely priced in; watching inventory" --conviction 3

    # 2. Fill the BEFORE block if you didn't inline it, THEN generate the report
    journal.py report NVDA

    # 3. Read the report, fill the AFTER block (impact: one of the four codes)

    # 4. Weeks later, record what actually happened
    journal.py outcome NVDA --date 2026-07-31

    # 5. Any time — see where you stand
    journal.py tally

Log EVERY case you open, including the boring ones. Logging only the impressive
hits is the fastest way to fool yourself.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ROOT / "journal" / "entries"
IMPACT_CODES = ("changed_thesis", "changed_confidence", "new_investigation", "no_value")


def _now() -> str:
    # UTC ISO; passed explicitly so the module has no hidden clock dependency.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _entry_path(ticker: str, day: str | None = None) -> Path:
    return ENTRIES / f"{ticker.upper()}_{day or _today()}.md"


def _find_entry(ticker: str, day: str | None) -> Path | None:
    if day:
        p = _entry_path(ticker, day)
        return p if p.exists() else None
    matches = sorted(ENTRIES.glob(f"{ticker.upper()}_*.md"))
    return matches[-1] if matches else None


def cmd_open(args: argparse.Namespace) -> int:
    ENTRIES.mkdir(parents=True, exist_ok=True)
    path = _entry_path(args.ticker)
    if path.exists():
        print(f"Entry already exists: {path}. Not overwriting.", file=sys.stderr)
        return 1
    thesis = args.thesis or "<one or two sentences: your view BEFORE reading the report>"
    conviction = args.conviction if args.conviction is not None else "<1-5>"
    action = args.action or "<hold / trim / add / avoid / no position>"
    path.write_text(
        f"# {args.ticker.upper()} — {_today()}\n"
        f"opened: {_now()}\n"
        f"reported:\n\n"
        f"## BEFORE  (write before reading the report)\n"
        f"thesis: {thesis}\n"
        f"conviction: {conviction}        # 1 (low) - 5 (high)\n"
        f"intended_action: {action}\n\n"
        f"## AFTER  (fill after reading the report)\n"
        f"impact:                         # any of: {', '.join(IMPACT_CODES)}\n"
        f"conviction_after:               # 1-5\n"
        f"what_it_surfaced:\n"
        f"what_i_disagreed_with:\n\n"
        f"## OUTCOME  (fill weeks later)\n"
        f"outcome_date:\n"
        f"what_happened:\n"
        f"verdict:                        # helped / neutral / hurt / too_early\n"
    )
    print(f"Opened {path}")
    print("Write your BEFORE block now (thesis + conviction), THEN run:")
    print(f"    journal.py report {args.ticker.upper()}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = _find_entry(args.ticker, args.date)
    if path is None:
        print(f"No open entry for {args.ticker.upper()}. Run `journal.py open` first.", file=sys.stderr)
        return 1
    text = path.read_text()
    thesis = re.search(r"^thesis:\s*(.*)$", text, re.MULTILINE)
    if not thesis or not thesis.group(1).strip() or thesis.group(1).strip().startswith("<"):
        print("BEFORE block looks empty — write your thesis first. Refusing to generate the "
              "report (that is the whole point).", file=sys.stderr)
        return 1
    # Only a value on the SAME line means already-reported. `\s` would span the
    # newline into the next heading and false-trip on every fresh entry.
    if re.search(r"^reported:[ \t]*\S", text, re.MULTILINE):
        print("This case's report was already generated; not regenerating "
              "(prevents peeking-then-editing).", file=sys.stderr)
        return 1
    text = re.sub(r"^reported:\s*$", f"reported: {_now()}", text, count=1, flags=re.MULTILINE)
    path.write_text(text)

    gen = ROOT / "scripts" / "generate_report.py"
    print(f"Thesis locked at {_now()}. Generating report...")
    rc = subprocess.call([sys.executable, str(gen), args.ticker.upper()]
                         + (["--no-docs"] if args.no_docs else []))
    if rc == 0:
        print(f"\nNow read the report and fill the AFTER block in {path}")
        print(f"  impact: one or more of {', '.join(IMPACT_CODES)}")
    return rc


def cmd_outcome(args: argparse.Namespace) -> int:
    path = _find_entry(args.ticker, args.date)
    if path is None:
        print(f"No entry found for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    print(f"Edit the OUTCOME block in {path} (outcome_date, what_happened, verdict).")
    return 0


def _field(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).split("#")[0].strip()
    return val or None


def cmd_tally(args: argparse.Namespace) -> int:
    entries = sorted(ENTRIES.glob("*.md")) if ENTRIES.exists() else []
    if not entries:
        print("No journal entries yet. Start with `journal.py open TICKER`.")
        return 0

    total = len(entries)
    impact_counts = dict.fromkeys(IMPACT_CODES, 0)
    conv_moved = conv_same = 0
    with_after = with_outcome = 0
    verdicts: dict[str, int] = {}
    missing_after: list[str] = []
    missing_outcome: list[str] = []

    for p in entries:
        text = p.read_text()
        name = p.stem
        impact = _field(text, "impact")
        if impact:
            with_after += 1
            for code in IMPACT_CODES:
                if code in impact:
                    impact_counts[code] += 1
        else:
            missing_after.append(name)
        cb, ca = _field(text, "conviction"), _field(text, "conviction_after")
        if cb and ca and cb.isdigit() and ca.isdigit():
            conv_moved += int(cb) != int(ca)
            conv_same += int(cb) == int(ca)
        verdict = _field(text, "verdict")
        if verdict:
            with_outcome += 1
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        else:
            missing_outcome.append(name)

    print(f"Journal tally — {total} cases logged ({with_after} scored, {with_outcome} with outcomes)\n")
    print("Impact (of scored cases):")
    for code in IMPACT_CODES:
        n = impact_counts[code]
        pct = f"{n / with_after:.0%}" if with_after else "—"
        print(f"  {code:20s} {n:3d}  ({pct})")
    changed_any = with_after - impact_counts["no_value"] if with_after else 0
    if with_after:
        print(f"\n  changed something (not no_value): {changed_any}/{with_after} "
              f"({changed_any / with_after:.0%})")
        print(f"  conviction moved: {conv_moved}, unchanged: {conv_same}")
    if verdicts:
        print("\nOutcomes (where recorded):")
        for v, n in sorted(verdicts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:12s} {n}")
    if missing_after:
        print(f"\n{len(missing_after)} cases still need an AFTER block: "
              f"{', '.join(missing_after[:8])}{' ...' if len(missing_after) > 8 else ''}")
    if missing_outcome:
        print(f"{len(missing_outcome)} cases still need an OUTCOME (revisit in a few weeks).")
    if total >= 20 and with_outcome >= 15:
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
