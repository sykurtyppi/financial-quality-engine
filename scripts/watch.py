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
    4  report generated but the audit FAILED — the report is kept for
       diagnosis, the journal entry is NOT marked reported (retryable)
    `due` returns 1 when a watched name still needs a thesis: that is the alert.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from statistics import median
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.sec_client import SecClient, SecClientError
from app.services.watch import watchlist as wl
from app.services.watch.infer import infer_print_at
from app.services.journal import store
from app.services.journal.schema_v2 import verify_lock
from app.services.watch.poller import (
    Gate,
    PollerError,
    decide,
    pinned_thesis_state,
    recent_filings,
)

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
        gate = pinned_thesis_state(w)
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
            print(f"    scripts/watch.py link {w.ticker}   # pin the entry to the event")
        return 1
    return 0


def _generate(ticker: str, entry_day: str | None, no_docs: bool) -> int:
    """Shell out to the journal CLI so the thesis-lock, timestamp and hash
    bookkeeping stay in exactly one implementation. Always --fresh: a <24h
    cached EDGAR answer can predate the filing this poll just detected.

    Always --defer-mark: `reported` is stamped only after the audit succeeds
    (see cmd_poll), so a failed audit leaves the case retryable. --date pins
    generation to the event's linked entry — never "latest entry wins".
    """
    cmd = [sys.executable, str(ROOT / "scripts" / "journal.py"), "report", ticker,
           "--fresh", "--defer-mark"]
    if entry_day:
        cmd += ["--date", entry_day]
    if no_docs:
        cmd.append("--no-docs")
    print(f"  -> {' '.join(cmd[1:])}")
    return subprocess.run(cmd, cwd=ROOT).returncode


def _mark_reported(ticker: str, entry_day: str | None) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "journal.py"), "mark-reported", ticker]
    if entry_day:
        cmd += ["--date", entry_day]
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


def _pin_for_adhoc(ticker: str, entry_day: str | None) -> tuple[str, str] | None:
    """For an ad-hoc (--since) poll: pin the journal entry explicitly named by
    --entry-day. Returns (day, before_sha256) or None. Never guesses "latest
    entry for the ticker" — that guess is how a stale prior-quarter thesis got
    attached to a different event."""
    if not entry_day:
        return None
    path = store.find_entry(ticker, entry_day)
    if path is None or not store.is_v2(path):
        print(f"{ticker}: --entry-day {entry_day} is not an existing v2 entry — "
              f"cannot pin.", file=sys.stderr)
        return None
    entry = store.load_v2(path)
    if not verify_lock(entry) or entry.before_sha256 is None:
        print(f"{path.name}: lock missing/broken — cannot pin.", file=sys.stderr)
        return None
    return entry_day, entry.before_sha256


def cmd_poll(args: argparse.Namespace) -> int:
    ticker = args.ticker.upper()
    watch = _find_watch(ticker)
    if watch is None and not args.since:
        print(f"{ticker} is not on the watchlist ({wl.WATCHLIST}) and no --since given.",
              file=sys.stderr)
        return 1
    if watch is None:
        pin = _pin_for_adhoc(ticker, args.entry_day)
        if args.entry_day and pin is None:
            return 1
        watch = wl.Watch(
            ticker=ticker,
            print_at=_now(args.since),
            thesis_entry=pin[0] if pin else None,
            thesis_sha256=pin[1] if pin else None,
        )
    # Operator-supplied lower bound only. The forecast print date must NEVER
    # act as a filing cutoff (an early filing would be excluded forever) —
    # without --since, filing identity comes from the watch's baseline
    # accession + expected report period.
    since = date.fromisoformat(args.since) if args.since else None

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
            decision = decide(watch, submissions, since=since,
                              force=getattr(args, "force", False))
        except PollerError as e:
            # Fail closed: a watch without event identity cannot poll safely.
            print(f"[{stamp}] {e}", file=sys.stderr)
            return 1
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
                entry_day = watch.thesis_entry
                rc = _generate(ticker, entry_day, args.no_docs)
                if rc != 0:
                    return rc
                if args.no_audit:
                    # No audit requested — generation completes the case.
                    return _mark_reported(ticker, entry_day)
                report = _latest_report(ticker, ROOT / "reports")
                if report is None:
                    print("  generated report not found under reports/ — cannot audit; "
                          "journal NOT marked reported.", file=sys.stderr)
                    return 4
                arc = _run_audit(report)
                if arc != 0:
                    # The report stays on disk for diagnosis; the entry stays
                    # unmarked so the case is retryable. A cron runner must see
                    # this as a failure, not a success with a missing audit.
                    print(f"  audit FAILED (exit {arc}); report kept at {report}; "
                          f"journal NOT marked reported — re-run the poll or "
                          f"`run_audit.py {report}` then "
                          f"`journal.py mark-reported {ticker}`.", file=sys.stderr)
                    return 4
                return _mark_reported(ticker, entry_day)
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
                    arc = _run_audit(report)
                    if arc != 0:
                        print(f"  audit FAILED (exit {arc}); auto-report kept at "
                              f"{report}.", file=sys.stderr)
                        return 4
                return 0

        if args.once:
            return 3  # "not yet" is not a failure; cron should not alert on it
        if time.monotonic() + args.interval > deadline:
            print(f"Gave up after {args.max_wait / 3600:.1f}h — no qualifying filing yet. "
                  f"Filing may be delayed; re-run or check EDGAR directly.", file=sys.stderr)
            return 1
        time.sleep(args.interval)


def _event_identity(
    submissions: dict, forms: tuple[str, ...],
) -> tuple[str, date | None]:
    """(baseline_accession, expected_report_date) from the filing history.

    Baseline = newest qualifying accession right now, so only a LATER accession
    can trigger. Expected period = newest periodic report period + the median
    in-band period gap (~91d) — what distinguishes THIS event's filing from an
    intervening earlier quarter's.
    """
    wanted = {f.upper() for f in forms}
    periodic = [
        f for f in recent_filings(submissions)
        if f.form.upper() in {"10-Q", "10-K"} and f.report_date is not None
    ]
    qualifying = [
        f for f in recent_filings(submissions)
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
    in_band = [g for g in gaps[-6:] if 75 <= g <= 105]
    step = int(median(in_band)) if in_band else 91
    return baseline, periods[-1] + timedelta(days=step)


def cmd_add(args: argparse.Namespace) -> int:
    """Ticker-only entry point: `watch.py add NVDA` and the calendar row is
    derived from the issuer's own filing history — print estimate from 8-K
    2.02 cadence (scheduling only), and the event identity (baseline
    accession + expected report period) that actually decides which filing
    counts."""
    ticker = args.ticker.upper()
    note = args.note
    try:
        client = SecClient()
        submissions = client.submissions_by_cik(client.resolve_cik(ticker))
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1

    if args.print_at:
        print_at = args.print_at
    else:
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

    forms = tuple(f.upper() for f in args.forms.split(",")) if args.forms else wl.DEFAULT_FORMS
    baseline, expected = _event_identity(submissions, forms)
    if args.expected_period:
        expected = date.fromisoformat(args.expected_period)
    if expected is None:
        print(f"{ticker}: no periodic filing history to infer the expected report "
              f"period from — pass --expected-period YYYY-MM-DD.", file=sys.stderr)
        return 1

    raw: dict = {
        "ticker": ticker,
        "print_at": print_at,
        "baseline_accession": baseline,
        "expected_report_date": expected.isoformat(),
    }
    if args.forms:
        raw["forms"] = list(forms)
    if args.label:
        raw["label"] = args.label
    if note:
        raw["note"] = note
    watch = wl.add_entry(raw)
    print(f"added {watch.ticker}: prints ~{watch.print_at:%Y-%m-%d %H:%M}Z (scheduling hint)")
    print(f"  event: expected period {expected.isoformat()}, "
          f"baseline accession {baseline or '(none)'}")
    if watch.note:
        print(f"  note: {watch.note}")
    print(f"  next: `journal.py openv2 {ticker} ...` then `watch.py link {ticker}`")
    return 0


def _linkable_entries(ticker: str) -> list[Path]:
    """Every v2 entry for `ticker` that could still back an event (unreported,
    loadable). Used to detect when `link` without --entry-day would be a guess
    between several candidates rather than the only possible resolution."""
    out: list[Path] = []
    for p in sorted(store.ENTRIES.glob(f"{store.safe_ticker(ticker)}_*.md")):
        if not store.is_v2(p):
            continue
        try:
            if store.load_v2(p).reported is None:
                out.append(p)
        except Exception:  # noqa: BLE001 — malformed entry: not linkable, not fatal
            continue
    return out


def cmd_link(args: argparse.Namespace) -> int:
    """Pin the event's journal entry (and its lock hash) onto the watch, so
    only THAT entry can ever authorize this event's report."""
    ticker = args.ticker.upper()
    watch = _find_watch(ticker)
    if watch is None:
        print(f"{ticker} is not on the watchlist ({wl.WATCHLIST}).", file=sys.stderr)
        return 1
    if not args.entry_day:
        # Without --entry-day, find_entry() picks the lexicographically latest
        # entry — the "latest entry wins" guess the pin mechanism exists to
        # eliminate. Resolution without the flag is safe only when exactly one
        # entry could possibly be pinned, and then it must be THAT entry —
        # falling through to find_entry here would re-pick the latest filename
        # (a spent or v1 entry included) and spuriously fail the routine
        # one-open-case workflow.
        candidates = _linkable_entries(ticker)
        if len(candidates) > 1:
            days = ", ".join(p.stem.split("_", 1)[1] for p in candidates)
            print(f"{ticker}: {len(candidates)} unreported v2 entries ({days}) — "
                  f"ambiguous which belongs to this event. Re-run with "
                  f"`--entry-day YYYY-MM-DD`.", file=sys.stderr)
            return 1
        # One candidate: pin it. Zero: fall through so the existing
        # no-entry/v1/reported error paths explain what is missing.
        path = candidates[0] if candidates else store.find_entry(ticker, None)
    else:
        path = store.find_entry(ticker, args.entry_day)
    if path is None:
        print(f"{ticker}: no journal entry"
              f"{' for ' + args.entry_day if args.entry_day else ''} — open one with "
              f"`journal.py openv2 {ticker} ...` first.", file=sys.stderr)
        return 1
    if not store.is_v2(path):
        print(f"{path.name}: only a hash-locked v2 entry can be pinned to an event "
              f"(the pin IS the hash). Use `journal.py openv2`.", file=sys.stderr)
        return 1
    entry = store.load_v2(path)
    if not verify_lock(entry) or entry.before_sha256 is None:
        print(f"{path.name}: lock missing or broken — refusing to pin.", file=sys.stderr)
        return 1
    if entry.reported is not None:
        print(f"{path.name}: already reported — a spent entry cannot back a new event.",
              file=sys.stderr)
        return 1
    day = path.stem.split("_", 1)[1]
    wl.update_entry(ticker, {"thesis_entry": day, "thesis_sha256": entry.before_sha256})
    print(f"pinned {path.name} (sha256 {entry.before_sha256[:12]}…) to the {ticker} watch")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    now = _now(args.now)
    watches = wl.load()
    if not watches:
        print(f"Watchlist is empty ({wl.WATCHLIST}).")
        return 0
    print(f"{'ticker':8} {'prints (UTC)':18} {'in':>9}  thesis")
    for w in watches:
        gate = pinned_thesis_state(w)
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
    p_poll.add_argument("--since", help="ad-hoc mode: only count filings on/after "
                        "YYYY-MM-DD (operator-supplied — a watchlist row uses its "
                        "baseline accession + expected period instead)")
    p_poll.add_argument("--entry-day", help="ad-hoc mode: pin this journal entry day "
                        "(YYYY-MM-DD) as the event's thesis")
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
    p_poll.add_argument("--force", action="store_true",
                        help="with --since on an armed watch: accept a filing that "
                             "does not match the watch's expected report period")
    p_poll.set_defaults(fn=cmd_poll)

    p_add = sub.add_parser("add", help="add a name; print date inferred from 8-K 2.02 cadence")
    p_add.add_argument("ticker")
    p_add.add_argument("--print-at", help="override: explicit ISO print time with offset")
    p_add.add_argument("--label", help='e.g. "FQ2-27"')
    p_add.add_argument("--note", help="free text; the inference basis is appended")
    p_add.add_argument("--forms", help='comma-separated watched forms (default "10-Q,10-K")')
    p_add.add_argument("--expected-period",
                       help="override the inferred fiscal period end (YYYY-MM-DD)")
    p_add.set_defaults(fn=cmd_add)

    p_link = sub.add_parser("link", help="pin a locked v2 journal entry to the watch")
    p_link.add_argument("ticker")
    p_link.add_argument("--entry-day", help="entry day YYYY-MM-DD (may be omitted only "
                        "when a single unreported v2 entry exists; refused when ambiguous)")
    p_link.set_defaults(fn=cmd_link)

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
