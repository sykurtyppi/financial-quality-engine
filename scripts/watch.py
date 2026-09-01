#!/usr/bin/env python3
"""Earnings-season watch: nudge before the print, poll for the filing after it.

    # ticker-only: add a name, print date inferred from its 8-K 2.02 cadence
    EDGAR_IDENTITY="Name email" scripts/watch.py add NVDA

    # what needs a thesis written before it reports?
    scripts/watch.py due --within-hours 36

    # after the print: wait for the filing, then run the report
    EDGAR_IDENTITY="Name email" scripts/watch.py poll NVDA

The poller will NOT generate a report for a name without a locked thesis — see
app/services/watch/poller.py for why that refusal is the point rather than an
inconvenience. Calendar lives in journal/watchlist.json.

Exit codes (for cron/alerting):
    0  work completed (report generated, or nothing left to do)
    1  setup problem, EDGAR failure, or the poll gave up waiting
    2  --no-auto only: filing landed but the thesis gate refused — you act.
       Without --no-auto, a thesis-less print instead produces a bannered
       non-journal artifact in reports/auto/ (the ticker-only track) and
       exits 0; a locked thesis always takes the journal track.
    3  still waiting: no qualifying filing yet (normal for a `--once` poll)
    `due` returns 1 when a watched name still needs a thesis: that is the alert.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.sec_client import SecClient, SecClientError
from app.services.watch import watchlist as wl
from app.services.watch.infer import infer_print_at
from app.services.watch.poller import Gate, decide, thesis_state

POLITE_INTERVAL_S = 300

AUTO_DIR = ROOT / "reports" / "auto"
AUTO_BANNER = (
    "> **AUTO-GENERATED AUDIT ARTIFACT** — no blind thesis was locked before "
    "this print; this report is NOT journal evidence (journal/JOURNAL.md "
    "rule 1) and lives outside `reports/` for that reason."
)


def _now(arg: str | None) -> datetime:
    """`--now` exists so the schedule can be rehearsed before the night it
    matters, rather than trusted on first contact with a live print."""
    if not arg:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(arg)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _find_watch(ticker: str) -> wl.Watch | None:
    return next((w for w in wl.load() if w.ticker == ticker.upper()), None)


def cmd_due(args: argparse.Namespace) -> int:
    now = _now(args.now)
    watches = wl.load()
    if not watches:
        print(f"Watchlist is empty ({wl.WATCHLIST}).")
        return 0

    upcoming = wl.due(watches, args.within_hours, now)
    if not upcoming:
        nxt = [w for w in watches if w.is_before_print(now)]
        if nxt:
            w = nxt[0]
            print(f"Nothing due within {args.within_hours:g}h. "
                  f"Next: {w.ticker} in {w.hours_until(now):.1f}h "
                  f"({w.print_at:%Y-%m-%d %H:%MZ}).")
        else:
            print("No upcoming prints on the watchlist.")
        return 0

    needs_thesis = []
    for w in upcoming:
        gate = thesis_state(w.ticker)
        label = f" [{w.label}]" if w.label else ""
        when = f"in {w.hours_until(now):.1f}h ({w.print_at:%Y-%m-%d %H:%MZ})"
        if gate.may_generate:
            print(f"  ready    {w.ticker}{label} — thesis locked, prints {when}")
        elif gate.state is Gate.ALREADY_REPORTED:
            print(f"  reported {w.ticker}{label} — {gate.detail}")
        else:
            needs_thesis.append(w)
            print(f"  NEEDS THESIS  {w.ticker}{label} — prints {when}")
            print(f"                {gate.detail}")

    if needs_thesis:
        print("\nWrite the prior BEFORE the print (journal/JOURNAL.md rule 1):")
        for w in needs_thesis:
            if w.note:
                print(f"    # {w.ticker}: {w.note}")
            print(f"    scripts/journal.py openv2 {w.ticker} --thesis \"...\" "
                  f"--conviction 3 --assumption \"...\"")
        return 1
    return 0


def _generate(ticker: str, no_docs: bool) -> int:
    """Shell out to the journal CLI so the thesis-lock, timestamp and hash
    bookkeeping stay in exactly one implementation. Always --fresh: a <24h
    cached EDGAR answer can predate the filing this poll just detected."""
    cmd = [sys.executable, str(ROOT / "scripts" / "journal.py"), "report", ticker, "--fresh"]
    if no_docs:
        cmd.append("--no-docs")
    print(f"  -> {' '.join(cmd[1:])}")
    return subprocess.run(cmd, cwd=ROOT).returncode


def _generate_auto(ticker: str, no_docs: bool) -> Path | None:
    """The non-journal track: generate the report into reports/auto/ with the
    NOT-journal-evidence banner. No thesis, no lock, no journal bookkeeping —
    and therefore never a blind case."""
    from app.services.journal import reporting

    try:
        out, _ = reporting.build_report(
            ticker, with_docs=not no_docs, fresh=True,
            out_dir=AUTO_DIR, banner=AUTO_BANNER,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  auto-report generation failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    print(f"  auto-report -> {out}")
    return out


def _latest_report(ticker: str, directory: Path) -> Path | None:
    matches = sorted(directory.glob(f"{ticker}_*.md"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def _run_audit(report: Path) -> int:
    """Headless audit loop over a generated report (scripts/run_audit.py)."""
    cmd = [sys.executable, str(ROOT / "scripts" / "run_audit.py"), str(report)]
    print(f"  -> {' '.join(cmd[1:])}")
    return subprocess.run(cmd, cwd=ROOT).returncode


def cmd_poll(args: argparse.Namespace) -> int:
    ticker = args.ticker.upper()
    watch = _find_watch(ticker)
    if watch is None and not args.since:
        print(f"{ticker} is not on the watchlist ({wl.WATCHLIST}) and no --since given.",
              file=sys.stderr)
        return 1
    if watch is None:
        watch = wl.Watch(ticker=ticker, print_at=_now(args.since))
    since = date.fromisoformat(args.since) if args.since else watch.print_at.date()

    try:
        client = SecClient(fresh=True)  # never a cached answer on a filing night
        cik = client.resolve_cik(ticker)
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.max_wait
    attempt = 0
    while True:
        attempt += 1
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        try:
            submissions = client.submissions_by_cik(cik)
            decision = decide(watch, submissions, since=since)
        except SecClientError as e:
            # A transient EDGAR failure must not end the watch; the filing may
            # still be minutes away. Surface it and retry.
            print(f"[{stamp}] attempt {attempt}: EDGAR error: {e}", file=sys.stderr)
            decision = None
        except Exception as e:  # noqa: BLE001
            print(f"[{stamp}] attempt {attempt}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

        if decision is not None:
            print(f"[{stamp}] attempt {attempt}: {decision.action} — {decision.message}")
            if decision.action == "generate":
                if args.dry_run:
                    print("  (dry run — not generating)")
                    return 0
                rc = _generate(ticker, args.no_docs)
                if rc == 0 and not args.no_audit:
                    report = _latest_report(ticker, ROOT / "reports")
                    if report is not None:
                        _run_audit(report)  # best-effort: report already exists
                return rc
            if decision.action == "skip":
                return 0
            if decision.action == "refuse":
                if args.no_auto:
                    return 2
                # Ticker-only track: no thesis was locked, so no blind case is
                # possible — generate the clearly-bannered auto artifact instead
                # of stopping. The journal gate itself is untouched.
                if args.dry_run:
                    print("  (dry run — would generate auto-report)")
                    return 0
                report = _generate_auto(ticker, args.no_docs)
                if report is None:
                    return 1
                if not args.no_audit:
                    _run_audit(report)
                return 0

        if args.once:
            return 3  # "not yet" is not a failure; cron should not alert on it
        if time.monotonic() + args.interval > deadline:
            print(f"Gave up after {args.max_wait / 3600:.1f}h — no qualifying filing yet. "
                  f"Filing may be delayed; re-run or check EDGAR directly.", file=sys.stderr)
            return 1
        time.sleep(args.interval)


def cmd_add(args: argparse.Namespace) -> int:
    """Ticker-only entry point: `watch.py add NVDA` and the calendar row is
    derived from the issuer's own 8-K 2.02 filing cadence."""
    ticker = args.ticker.upper()
    note = args.note
    if args.print_at:
        print_at = args.print_at
    else:
        try:
            client = SecClient()
            submissions = client.submissions_by_cik(client.resolve_cik(ticker))
        except SecClientError as e:
            print(f"EDGAR unavailable: {e}", file=sys.stderr)
            return 1
        est = infer_print_at(submissions)
        if est is None:
            print(
                f"{ticker}: cannot infer the print date — needs >=3 regular 8-K "
                f"Item 2.02 filings in recent history. Pass --print-at explicitly.",
                file=sys.stderr,
            )
            return 1
        print_at = est.print_at.isoformat()
        note = f"{args.note} · {est.basis}" if args.note else est.basis

    raw: dict = {"ticker": ticker, "print_at": print_at}
    if args.label:
        raw["label"] = args.label
    if note:
        raw["note"] = note
    watch = wl.add_entry(raw)
    print(f"added {watch.ticker}: prints {watch.print_at:%Y-%m-%d %H:%M}Z")
    if watch.note:
        print(f"  note: {watch.note}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    now = _now(args.now)
    watches = wl.load()
    if not watches:
        print(f"Watchlist is empty ({wl.WATCHLIST}).")
        return 0
    print(f"{'ticker':8} {'prints (UTC)':18} {'in':>9}  thesis")
    for w in watches:
        gate = thesis_state(w.ticker)
        hrs = w.hours_until(now)
        when = f"{hrs:.1f}h" if hrs > 0 else "past"
        print(f"{w.ticker:8} {w.print_at:%Y-%m-%d %H:%MZ}  {when:>9}  {gate.state.value}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    p_due = sub.add_parser("due", help="names reporting soon that still need a thesis")
    p_due.add_argument("--within-hours", type=float, default=36.0)
    p_due.add_argument("--now", help="override 'now' (ISO) for rehearsal")
    p_due.set_defaults(fn=cmd_due)

    p_poll = sub.add_parser("poll", help="wait for the filing, then generate (thesis-gated)")
    p_poll.add_argument("ticker")
    p_poll.add_argument("--since", help="only count filings on/after YYYY-MM-DD (default: print date)")
    p_poll.add_argument("--interval", type=float, default=POLITE_INTERVAL_S,
                        help=f"seconds between EDGAR checks (default {POLITE_INTERVAL_S})")
    p_poll.add_argument("--max-wait", type=float, default=6 * 3600,
                        help="give up after this many seconds (default 6h)")
    p_poll.add_argument("--once", action="store_true", help="check once and exit")
    p_poll.add_argument("--dry-run", action="store_true", help="detect but do not generate")
    p_poll.add_argument("--no-docs", action="store_true", help="pass through to report generation")
    p_poll.add_argument("--no-auto", action="store_true",
                        help="strict journal mode: refuse (exit 2) instead of generating "
                             "the bannered reports/auto/ artifact when no thesis is locked")
    p_poll.add_argument("--no-audit", action="store_true",
                        help="skip the headless earnings-audit run after generation")
    p_poll.set_defaults(fn=cmd_poll)

    p_add = sub.add_parser("add", help="add a name; print date inferred from 8-K 2.02 cadence")
    p_add.add_argument("ticker")
    p_add.add_argument("--print-at", help="override: explicit ISO print time with offset")
    p_add.add_argument("--label", help='e.g. "FQ2-27"')
    p_add.add_argument("--note", help="free text; the inference basis is appended")
    p_add.set_defaults(fn=cmd_add)

    p_st = sub.add_parser("status", help="watchlist + thesis state at a glance")
    p_st.add_argument("--now", help="override 'now' (ISO) for rehearsal")
    p_st.set_defaults(fn=cmd_status)

    args = p.parse_args()
    try:
        return args.fn(args)
    except wl.WatchlistError as e:
        print(f"Watchlist error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
