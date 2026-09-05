#!/usr/bin/env python3
"""Earnings-season watch: nudge before the print, poll for the filing after it.

    # ticker-only: add a name, print date inferred from its 8-K 2.02 cadence
    EDGAR_IDENTITY="Name email" scripts/watch.py add NVDA

    # what needs a thesis written before it reports?
    scripts/watch.py due --within-hours 36

    # after the print: wait for the filing, then run the report
    EDGAR_IDENTITY="Name email" scripts/watch.py poll NVDA

    # hands-off: one pass over every watched name (cron this hourly). Adds
    # anything new in journal/portfolio.txt first, generates + audits whatever
    # has filed, and re-arms each name for its next quarter.
    EDGAR_IDENTITY="Name email" scripts/watch.py sweep --portfolio journal/portfolio.txt

The poller will NOT generate a *journal* report for a name without a locked
thesis — see app/services/watch/poller.py for why that refusal is the point
rather than an inconvenience; a thesis-less print takes the bannered auto
track instead. Calendar lives in journal/watchlist.json. After any completed
event (journal or auto) the watch is RE-ARMED from the issuer's filing
history — new baseline accession, next expected period, next print hint, pin
cleared — so a name never has to be added twice.

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
    `sweep` returns the worst per-name code, except that 3 (waiting) is 0 and
    a sweep already running elsewhere is 0 (it just yields).
"""

from __future__ import annotations

import argparse
import fcntl
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.sec_client import SecClient, SecClientError
from app.services.watch import watchlist as wl
from app.services.watch.infer import infer_print_at
from app.services.watch.rearm import event_identity, next_arming
from app.services.journal import store
from app.services.journal.schema_v2 import verify_lock
from app.services.watch.poller import (
    Gate,
    PollerError,
    decide,
    pinned_thesis_state,
)

POLITE_INTERVAL_S = 300

AUTO_DIR = ROOT / "reports" / "auto"
PORTFOLIO = ROOT / "journal" / "portfolio.txt"
SWEEP_LOCK = ROOT / "journal" / "sweep.lock"
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


def _act(ticker: str, watch: wl.Watch, decision, args: argparse.Namespace) -> int:
    """Carry out a non-wait decision. Exit-code semantics are the module
    docstring's; shared by `poll` and `sweep` so the two can never drift."""
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
    print(f"  unknown decision {decision.action!r}", file=sys.stderr)
    return 1


def _rearm(watch: wl.Watch, decision, submissions: dict) -> None:
    """Re-arm a watchlist row for its next quarter once this event is done.

    Only called after a COMPLETED event (exit 0 on either track, or skip):
    a failed audit (exit 4) leaves the identity in place so the next pass
    retries the same filing. Persistence errors are reported, never raised —
    the report already exists; a stale calendar row is the lesser problem.
    """
    try:
        arming = next_arming(
            submissions, watch.forms,
            filed=decision.filing, previous_expected=watch.expected_report_date,
        )
        wl.update_entry(watch.ticker, arming.as_updates())
    except (wl.WatchlistError, PollerError) as e:
        print(f"  re-arm FAILED for {watch.ticker}: {e} — the watch still names "
              f"the consumed event; re-`add` it.", file=sys.stderr)
        return
    print(f"  re-armed {watch.ticker}: next period ~{arming.expected_report_date}, "
          f"baseline {arming.baseline_accession or '(none)'}, "
          f"prints ~{arming.print_at:%Y-%m-%d %H:%M}Z (pin cleared)")


def _rearm_guarded(watch: wl.Watch, decision, submissions: dict) -> None:
    """Re-arm after a completed event without letting ANY persistence crash
    (disk full, permissions) turn an already-generated, audited, marked case
    into a traceback — on the sweep it would abort the pass, on a poll it
    would hide a successful run behind a crash exit."""
    try:
        _rearm(watch, decision, submissions)
    except Exception as e:  # noqa: BLE001
        print(f"  re-arm FAILED for {watch.ticker}: {type(e).__name__}: {e} — the watch "
              f"still names the consumed event; re-`add` it.", file=sys.stderr)


def _completed(decision, rc: int) -> bool:
    return decision.action == "skip" or (decision.action in ("generate", "refuse") and rc == 0)


def cmd_poll(args: argparse.Namespace) -> int:
    ticker = args.ticker.upper()
    watch = _find_watch(ticker)
    adhoc = watch is None
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
            if decision.action != "wait":
                if args.dry_run:
                    return _act(ticker, watch, decision, args)
                with _activity_lock(timeout=deadline - time.monotonic()) as held:
                    if not held:
                        print(f"another sweep or poll held the activity lock ({SWEEP_LOCK}) "
                              f"for the rest of --max-wait — giving up; re-run.", file=sys.stderr)
                        return 1
                    # A sweep may have consumed (and re-armed) this event
                    # while we waited for the lock: re-read the row and
                    # re-decide before acting. A row whose identity moved
                    # was re-armed by that run — consumed, whatever
                    # `--since` would still match.
                    if not adhoc:
                        current = _find_watch(ticker) or watch
                        if (current.baseline_accession, current.expected_report_date) != (
                            watch.baseline_accession, watch.expected_report_date
                        ):
                            print(f"  {ticker}: re-armed by a concurrent run (baseline now "
                                  f"{current.baseline_accession or '(none)'}) — nothing to do.")
                            return 0
                        watch = current
                    try:
                        submissions = client.submissions_by_cik(cik)
                        decision = decide(watch, submissions, since=since,
                                          force=getattr(args, "force", False))
                    except (PollerError, SecClientError) as e:
                        print(f"  re-check under lock failed: {e}", file=sys.stderr)
                        return 1
                    if decision.action == "wait":
                        print(f"  {decision.message} — consumed by a concurrent run; nothing to do.")
                        return 0
                    rc = _act(ticker, watch, decision, args)
                    if not adhoc and _completed(decision, rc):
                        _rearm_guarded(watch, decision, submissions)
                    return rc

        if args.once:
            return 3  # "not yet" is not a failure; cron should not alert on it
        if time.monotonic() + args.interval > deadline:
            print(f"Gave up after {args.max_wait / 3600:.1f}h — no qualifying filing yet. "
                  f"Filing may be delayed; re-run or check EDGAR directly.", file=sys.stderr)
            return 1
        time.sleep(args.interval)


def _sweep_one(client: SecClient, watch: wl.Watch, args: argparse.Namespace) -> int:
    """One pass for one watch: fetch, decide, act, re-arm. Never raises —
    a sweep must reach every name even when one of them fails."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    try:
        submissions = client.submissions_by_cik(client.resolve_cik(watch.ticker))
        decision = decide(watch, submissions)
    except (PollerError, SecClientError) as e:
        print(f"[{stamp}] {watch.ticker}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[{stamp}] {watch.ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if decision.action == "wait":
        if args.verbose:
            print(f"[{stamp}] {decision.message}")
        return 3
    print(f"[{stamp}] {decision.action} — {decision.message}")
    try:
        rc = _act(watch.ticker, watch, decision, args)
    except Exception as e:  # noqa: BLE001
        print(f"  {watch.ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not args.dry_run and _completed(decision, rc):
        _rearm_guarded(watch, decision, submissions)
    return rc


LOCK_RETRY_S = 0.5


@contextmanager
def _activity_lock(*, timeout: float):
    """One generate/audit/re-arm at a time across `sweep` and `poll`.

    Both can otherwise decide "generate" for the same landed filing — the
    cron sweep and a manually started poll — and run two full fetch+audit
    cycles into the same report path. `sweep` takes it with timeout 0
    (yields with exit 0 when another run is acting); `poll` takes it only
    once it has something to act on, waiting at most what is left of its
    own --max-wait, then re-reads the row and re-decides, so a filing the
    sweep already consumed is a skip rather than a duplicate. Yields True
    when held, False when the wait ran out.
    """
    SWEEP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    give_up = time.monotonic() + max(timeout, 0.0)
    with open(SWEEP_LOCK, "w") as lock_fh:
        while True:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    yield False
                    return
                time.sleep(min(LOCK_RETRY_S, max(give_up - time.monotonic(), 0.01)))
        try:
            yield True
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def cmd_sweep(args: argparse.Namespace) -> int:
    """The hands-off entry point: one pass over the whole calendar.

    Runs `sync` first when --portfolio is given, so the cron line is the only
    per-season setup. A second sweep starting while one is still auditing
    (an audit can outlast an hourly interval) yields immediately instead of
    generating the same report twice.
    """
    with _activity_lock(timeout=0) as held:
        if not held:
            print(f"another sweep or poll is acting ({SWEEP_LOCK}) — yielding.")
            return 0
        return _sweep_locked(args)


def _sweep_locked(args: argparse.Namespace) -> int:
    try:
        client = SecClient(fresh=True)
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    worst = 0
    if args.portfolio is not None:
        worst = _sync(client, Path(args.portfolio), prune=args.prune, dry_run=args.dry_run)
    watches = wl.load()
    if not watches:
        print(f"Watchlist is empty ({wl.WATCHLIST}).")
        return worst
    results: dict[str, int] = {}
    for w in watches:
        results[w.ticker] = _sweep_one(client, w, args)
    acted = {t: rc for t, rc in results.items() if rc != 3}
    waiting = len(results) - len(acted)
    print(f"sweep: {len(results)} watched, {waiting} waiting"
          + (", " + ", ".join(f"{t} -> {rc}" for t, rc in acted.items()) if acted else ""))
    return max([worst, *acted.values()]) if acted else worst


def _arm(
    ticker: str,
    submissions: dict,
    *,
    print_at: str | None = None,
    forms: tuple[str, ...] | None = None,
    label: str | None = None,
    note: str | None = None,
    expected_period: date | None = None,
) -> wl.Watch:
    """Derive a calendar row from the issuer's own filing history and append
    it: print estimate from 8-K 2.02 cadence (scheduling only), and the event
    identity (baseline accession + expected report period) that actually
    decides which filing counts. Raises WatchlistError on anything that
    cannot be derived, naming the override flag."""
    if print_at is None:
        est = infer_print_at(submissions)
        if est is None:
            raise wl.WatchlistError(
                f"{ticker}: cannot infer the print date — needs >=3 regular 8-K "
                f"Item 2.02 filings in recent history. Pass --print-at explicitly."
            )
        print_at = est.print_at.isoformat()
        note = f"{note} · {est.basis}" if note else est.basis

    forms = forms or wl.DEFAULT_FORMS
    baseline, expected = event_identity(submissions, forms)
    if expected_period is not None:
        expected = expected_period
    if expected is None:
        raise wl.WatchlistError(
            f"{ticker}: no periodic filing history to infer the expected report "
            f"period from — pass --expected-period YYYY-MM-DD."
        )
    raw: dict = {
        "ticker": ticker,
        "print_at": print_at,
        "baseline_accession": baseline,
        "expected_report_date": expected.isoformat(),
    }
    if forms != wl.DEFAULT_FORMS:
        raw["forms"] = list(forms)
    if label:
        raw["label"] = label
    if note:
        raw["note"] = note
    return wl.add_entry(raw)


def cmd_add(args: argparse.Namespace) -> int:
    """Ticker-only entry point: `watch.py add NVDA` and the calendar row is
    derived from the issuer's own filing history."""
    ticker = args.ticker.upper()
    try:
        client = SecClient()
        submissions = client.submissions_by_cik(client.resolve_cik(ticker))
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    forms = tuple(f.upper() for f in args.forms.split(",")) if args.forms else None
    expected = date.fromisoformat(args.expected_period) if args.expected_period else None
    try:
        watch = _arm(ticker, submissions, print_at=args.print_at, forms=forms,
                     label=args.label, note=args.note, expected_period=expected)
    except wl.WatchlistError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"added {watch.ticker}: prints ~{watch.print_at:%Y-%m-%d %H:%M}Z (scheduling hint)")
    print(f"  event: expected period {watch.expected_report_date}, "
          f"baseline accession {watch.baseline_accession or '(none)'}")
    if watch.note:
        print(f"  note: {watch.note}")
    print(f"  next: `journal.py openv2 {ticker} ...` then `watch.py link {ticker}`")
    return 0


def read_portfolio(path: Path) -> list[str]:
    """Tickers from a holdings file: one per line, `#` comments; anything
    after the first comma/whitespace is ignored so a line like
    `NVDA, 100 sh` works. No header-row detection — strip one from a
    brokerage export first, or it is reported as an unknown ticker."""
    if not path.is_file():
        raise wl.WatchlistError(f"portfolio file not found: {path}")
    out: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.replace(",", " ").split()[0].strip().strip('"')
        try:
            t = store.safe_ticker(token)
        except ValueError:
            continue  # header row, currency line, etc.
        if t not in out:
            out.append(t)
    return out


def _sync(client: SecClient, portfolio: Path, *, prune: bool, dry_run: bool = False) -> int:
    """Make the watchlist cover the portfolio: arm every holding not yet
    watched; with --prune, drop watched names no longer held — except a name
    with a thesis pinned, which is an event in flight, not a stale row.
    `dry_run` reports every add/remove it would make and writes nothing."""
    try:
        wanted = read_portfolio(portfolio)
    except wl.WatchlistError as e:
        print(str(e), file=sys.stderr)
        return 1
    watched = {w.ticker: w for w in wl.load()}
    rc = 0
    added = 0
    would = "would add" if dry_run else "added"
    for t in wanted:
        if t in watched:
            continue
        if dry_run:
            added += 1
            print(f"sync: {would} {t}")
            continue
        try:
            submissions = client.submissions_by_cik(client.resolve_cik(t))
            w = _arm(t, submissions)
        except (SecClientError, wl.WatchlistError) as e:
            print(f"sync: {t} NOT added — {e}", file=sys.stderr)
            rc = 1
            continue
        added += 1
        print(f"sync: {would} {t}: prints ~{w.print_at:%Y-%m-%d %H:%M}Z, "
              f"period ~{w.expected_report_date}")
    stale = [w for t, w in watched.items() if t not in wanted]
    for w in stale:
        if not prune:
            print(f"sync: {w.ticker} is watched but not in {portfolio.name} (keep; --prune removes)")
        elif w.thesis_entry:
            print(f"sync: {w.ticker} not in {portfolio.name} but has a pinned thesis — kept")
        elif dry_run:
            print(f"sync: would remove {w.ticker}")
        else:
            wl.remove_entry(w.ticker)
            print(f"sync: removed {w.ticker}")
    print(f"sync{' (dry run)' if dry_run else ''}: {len(wanted)} in portfolio, {added} {would}, "
          f"{len(stale)} watched-but-not-held")
    return rc


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        client = SecClient()
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    return _sync(client, Path(args.portfolio), prune=args.prune, dry_run=args.dry_run)


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

    p_sw = sub.add_parser("sweep", help="one hands-off pass over every watched name "
                          "(generate, audit, re-arm); cron this")
    p_sw.add_argument("--portfolio", nargs="?", const=str(PORTFOLIO), default=None,
                      help=f"sync from this holdings file first (default {PORTFOLIO.name})")
    p_sw.add_argument("--prune", action="store_true",
                      help="with --portfolio: drop watched names no longer held (unpinned only)")
    p_sw.add_argument("--dry-run", action="store_true", help="detect but do not generate")
    p_sw.add_argument("--no-docs", action="store_true", help="pass through to report generation")
    p_sw.add_argument("--no-auto", action="store_true",
                      help="strict journal mode: refuse (2) instead of the reports/auto/ artifact")
    p_sw.add_argument("--no-audit", action="store_true",
                      help="skip the headless earnings-audit run after generation")
    p_sw.add_argument("--verbose", action="store_true", help="also print names still waiting")
    p_sw.set_defaults(fn=cmd_sweep)

    p_sync = sub.add_parser("sync", help="arm every holding in a portfolio file")
    p_sync.add_argument("--portfolio", default=str(PORTFOLIO),
                        help=f"holdings file, one ticker per line (default {PORTFOLIO})")
    p_sync.add_argument("--prune", action="store_true",
                        help="drop watched names no longer held (unpinned only)")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="report what would be armed/removed; write nothing")
    p_sync.set_defaults(fn=cmd_sync)

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
