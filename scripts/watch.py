#!/usr/bin/env python3
"""Earnings-season watch: nudge before the print, poll for the filing after it.

    # ticker-only: add a name, print date inferred from its 8-K 2.02 cadence
    EDGAR_IDENTITY="Name email" scripts/watch.py add NVDA

    # what needs a thesis written before it reports?
    scripts/watch.py due --within-hours 36

    # after the print: wait for the filing, then run the report
    EDGAR_IDENTITY="Name email" scripts/watch.py poll NVDA

    # hands-off: one pass over every watched name (cron this hourly). Adds
    # anything new in journal/portfolio.txt first, generates + audits + briefs
    # whatever has filed, and re-arms each name for its next quarter.
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
    5  case completed (report, audit, mark, re-arm) but the BRIEF failed —
       queued under reports/briefs/.pending/ and retried by every later
       sweep pass until it succeeds; the print is never silently brief-less.
       Also: a PRINT-NIGHT brief (8-K-triggered, see below) failed and is
       queued the same way.

Print night vs 10-Q: the engine report needs the quarter's XBRL, so the
report/audit track fires on the 10-Q/10-K. The brief is a read of the
print itself, so `sweep` ALSO fires a brief-only pass on the earnings 8-K
(Item 2.02) the hour it lands — release + call transcript, engine findings
UNAVAILABLE — and rebuilds that brief with the engine findings when the
10-Q lands (the `useful:` value carries over). For NVDA the two are minutes
apart; for a small cap the 10-Q can be weeks later.
    `due` returns 1 when a watched name still needs a thesis: that is the alert.
    A completed event whose RE-ARM failed also returns 1: the report exists,
    but the row still names the consumed event and will never fire again
    until it is re-`add`ed — a scheduler must see that.
    `sweep` returns the worst per-name code — worst by severity, not by
    number: 1 (setup/EDGAR) > 4 (audit failed) > 2 (refused) > 5 (brief
    queued) > 0 — except that 3 (waiting) is 0 and a sweep already running
    elsewhere is 0 (it just yields). A name still
    waiting more than OVERDUE_DAYS past its print hint is named on stderr on
    every pass, --verbose or not: "waiting" must not hide a mis-armed row.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.brief.sources import BRIEFS, BriefSourceError, latest_earnings_8k
from app.services.ingestion.vintages import capture as capture_vintage
from app.services.delivery import notify
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
BRIEF_PENDING = ROOT / "reports" / "briefs" / ".pending"  # <TICKER>: target line + attempts line
BRIEF_PENDING_RC = 5
NO_REPORT_MARK = "-"  # queue target for a print-night brief (no engine report yet)
PRINT_BRIEF_WINDOW_DAYS = 14  # an earnings 8-K older than this is last quarter's, not news
# A brief that keeps failing is retried this many times (hourly passes), then
# left alone: each retry is a paid headless run, and a DETERMINISTIC failure
# (the model drifting from the brief contract, a source that cannot be built)
# never gets better by running it again. A print-night brief is then dropped —
# the 10-Q rebuild is its second chance. A FULL brief has no later rebuild, so
# its queue entry is KEPT and every pass keeps saying so (exit 5, notified)
# without spending anything: giving up retrying is not giving up telling you.
PRINT_BRIEF_MAX_ATTEMPTS = 6
BRIEF_MAX_ATTEMPTS = PRINT_BRIEF_MAX_ATTEMPTS
# Sweep aggregate: the worst code across names, by what it means rather than
# by its number — a queued brief (5) must never outrank a failed audit (4) on
# another name, or an alert keyed on the exit code would miss the audit.
SEVERITY_ORDER = (1, 4, 2, 5, 0)


def _worst(codes) -> int:
    """Worst sweep code by severity; 3 (still waiting) counts as 0."""
    codes = {0 if c == 3 else c for c in codes}
    return next((c for c in SEVERITY_ORDER if c in codes), max(codes, default=0))
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    """Newest engine report for the ticker in `directory` — never the audit
    written beside it (`<stem>_audit.md`), which a retry after a failed
    brief would otherwise hand to the auditor as "the report"."""
    matches = sorted(
        (p for p in directory.glob(f"{ticker}_*.md") if not p.stem.endswith("_audit")),
        key=lambda p: p.stat().st_mtime,
    )
    return matches[-1] if matches else None


def _run_audit(report: Path) -> int:
    """Headless audit loop over a generated report (scripts/run_audit.py)."""
    cmd = [sys.executable, str(ROOT / "scripts" / "run_audit.py"), str(report)]
    print(f"  -> {' '.join(cmd[1:])}")
    return subprocess.run(cmd, cwd=ROOT).returncode


def _run_brief(ticker: str, report: Path | None) -> int:
    """One-page earnings brief (scripts/earnings_brief.py) over the release,
    the call transcript if one was dropped in, and this report + audit —
    or, with `report=None`, the print-night variant (release + call only).

    The report and audit already exist, so a failed brief does not un-complete
    the case (the row is still re-armed). But it is not just a warning either:
    the failure is QUEUED (reports/briefs/.pending/<TICKER> names the report)
    and every later sweep pass retries it first, so a season-long fault — the
    CLI missing from a scheduler's PATH, an expired login — cannot quietly
    leave every print brief-less behind a green exit code."""
    cmd = [sys.executable, str(ROOT / "scripts" / "earnings_brief.py"), "build", ticker]
    cmd += ["--no-report"] if report is None else ["--report", str(report)]
    print(f"  -> {' '.join(cmd[1:])}")
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    marker = BRIEF_PENDING / ticker
    if rc != 0:
        target = NO_REPORT_MARK if report is None else str(report)
        prior = _queue_read(ticker)
        attempts = (prior[1] + 1) if prior and prior[0] == target else 1
        _queue_write(ticker, target, attempts)  # a new failure re-arms the alert
        print(f"  brief FAILED (exit {rc}) — queued at {marker} (attempt {attempts}) and "
              f"retried on the next sweep pass (or run "
              f"`earnings_brief.py {' '.join(cmd[2:])}` by hand).", file=sys.stderr)
    elif marker.exists():
        marker.unlink()
    return rc


def _queue_read(ticker: str) -> tuple[str, int, str] | None:
    """(target, attempts, alerted) from the queue marker: target is a report
    path or NO_REPORT_MARK, `alerted` is the YYYY-MM-DD an exhausted entry
    last raised a notification (empty when it never has). None when nothing
    is queued. A marker written before `alerted` existed reads as ""."""
    marker = BRIEF_PENDING / ticker
    if not marker.is_file():
        return None
    lines = marker.read_text().splitlines()
    target = lines[0].strip() if lines else ""
    attempts, alerted = 1, ""
    for line in lines[1:]:
        if line.startswith("attempts="):
            try:
                attempts = int(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("alerted="):
            alerted = line.split("=", 1)[1].strip()
    return (target, attempts, alerted) if target else None


def _queue_write(ticker: str, target: str, attempts: int, alerted: str = "") -> None:
    BRIEF_PENDING.mkdir(parents=True, exist_ok=True)
    body = f"{target}\nattempts={attempts}\n" + (f"alerted={alerted}\n" if alerted else "")
    (BRIEF_PENDING / ticker).write_text(body)


def _retry_pending_brief(ticker: str) -> int:
    """Re-run a brief queued by an earlier failure. 0 when nothing is queued
    or the retry succeeded; BRIEF_PENDING_RC when it failed again."""
    marker = BRIEF_PENDING / ticker
    queued = _queue_read(ticker)
    if queued is None:
        return 0
    target, attempts, alerted = queued
    report: Path | None = None if target == NO_REPORT_MARK else Path(target)
    if report is not None and not report.is_file():
        print(f"  {ticker}: queued brief names a report that no longer exists "
              f"({report}) — dropping the queue entry.", file=sys.stderr)
        marker.unlink()
        return 0
    if attempts >= BRIEF_MAX_ATTEMPTS:
        if report is None:
            print(f"  {ticker}: print-night brief failed {attempts} times — giving up on it; "
                  f"the brief is built with the engine findings when the 10-Q lands, or run "
                  f"`earnings_brief.py build {ticker} --no-report` by hand.", file=sys.stderr)
            marker.unlink()
        else:
            # Kept, not retried: nothing else will produce this brief. It is
            # logged every pass, but it only ALERTS once a day — an hourly
            # notification about a state that cannot change on its own is
            # noise, and noise is how a real alert gets ignored.
            print(f"  {ticker}: brief for {report.name} failed {attempts} times — no longer "
                  f"retrying (a repeated failure is deterministic: run "
                  f"`earnings_brief.py build {ticker} --report {report}` to see the error). "
                  f"Queued at {marker}; delete it to silence this.", file=sys.stderr)
            today = _utcnow().date().isoformat()
            if alerted == today:
                return 0  # already raised today; the log line above is the record
            _queue_write(ticker, target, attempts, alerted=today)
        return BRIEF_PENDING_RC
    print(f"  {ticker}: retrying the queued "
          f"{'print-night brief' if report is None else 'brief for ' + report.name}"
          f" (attempt {attempts + 1})")
    return BRIEF_PENDING_RC if _run_brief(ticker, report) != 0 else 0


def _print_night_brief(watch: wl.Watch, submissions: dict, args: argparse.Namespace,
                       now: datetime) -> int:
    """Brief-only pass on the earnings 8-K, independent of the 10-Q track.

    Fires once per print: the newest Item 2.02 8-K, filed within
    PRINT_BRIEF_WINDOW_DAYS, with no brief on disk for its date and nothing
    queued. The brief's filename IS the idempotency key — `<TICKER>_<8-K
    filing date>.md` — so no watchlist state is added; when the 10-Q hook
    later rebuilds the same file with the engine findings, this pass sees it
    exists and stays quiet. 0, or BRIEF_PENDING_RC when the run failed
    (queued). Never raises."""
    if getattr(args, "no_brief", False) or getattr(args, "no_auto", False):
        # --no-auto is "nothing unattended for a thesis-less name": that
        # covers the brief too.
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M:%SZ")
    try:
        k = latest_earnings_8k(submissions)
    except (BriefSourceError, PollerError) as e:
        if args.verbose:
            print(f"[{stamp}] {watch.ticker}: no earnings 8-K to brief ({e})")
        return 0
    # UTC calendar days against EDGAR's US-Eastern filing date: at most a
    # day of skew at the edge of a two-week window.
    if (now.date() - k.filing_date).days > PRINT_BRIEF_WINDOW_DAYS:
        return 0
    day = k.filing_date.isoformat()
    superseded = None
    if (BRIEFS / f"{watch.ticker}_{day}.md").exists():
        meta = _built_meta(watch.ticker, day)
        # Same day, second 2.02 8-K (preliminary then final): a print-night
        # brief built from the earlier accession is rebuilt from the newer
        # one; a full brief — or one with no record — is never touched here.
        if meta is None or meta.get("kind") != "print-night" \
                or meta.get("accession") == k.accession:
            return 0
        superseded = meta.get("accession")
    if (BRIEF_PENDING / watch.ticker).exists():
        return 0  # already queued by an earlier failure; the retry owns it
    if superseded:
        print(f"[{stamp}] {watch.ticker}: a newer earnings 8-K {k.accession} superseded "
              f"{superseded} the same day — rebuilding the print-night brief")
    print(f"[{stamp}] {watch.ticker}: earnings 8-K {k.accession} filed {k.filing_date} — "
          f"print-night brief (engine findings follow with the 10-Q)")
    if args.dry_run:
        print("  (dry run — not building)")
        return 0
    return BRIEF_PENDING_RC if _run_brief(watch.ticker, None) != 0 else 0


def _built_meta(ticker: str, day: str) -> dict | None:
    """The brief's build record (reports/briefs/<T>/<day>/built.json), or None."""
    try:
        meta = json.loads((BRIEFS / ticker / day / "built.json").read_text())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _print_night_guarded(watch, submissions, args, now) -> int:
    try:
        return _print_night_brief(watch, submissions, args, now)
    except Exception as e:  # noqa: BLE001 — never takes the pass down
        print(f"  {watch.ticker}: print-night brief crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        try:
            # Keep the queue an honest record: the next pass retries it there.
            if _queue_read(watch.ticker) is None:
                _queue_write(watch.ticker, NO_REPORT_MARK, 1)
        except OSError:
            pass
        return BRIEF_PENDING_RC


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
        brief_rc = 0
        if not getattr(args, "no_brief", False):
            brief_rc = _run_brief(ticker, report)
        rc = _mark_reported(ticker, entry_day)
        return rc if rc != 0 else (BRIEF_PENDING_RC if brief_rc != 0 else 0)
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
            if not getattr(args, "no_brief", False) and _run_brief(ticker, report) != 0:
                return BRIEF_PENDING_RC
        return 0
    print(f"  unknown decision {decision.action!r}", file=sys.stderr)
    return 1


def _rearm(watch: wl.Watch, decision, submissions: dict) -> bool:
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
        return False
    print(f"  re-armed {watch.ticker}: next period ~{arming.expected_report_date}, "
          f"baseline {arming.baseline_accession or '(none)'}, "
          f"prints ~{arming.print_at:%Y-%m-%d %H:%M}Z (pin cleared)")
    return True


def _rearm_guarded(watch: wl.Watch, decision, submissions: dict) -> bool:
    """Re-arm after a completed event without letting ANY persistence crash
    (disk full, permissions) turn an already-generated, audited, marked case
    into a traceback — on the sweep it would abort the pass, on a poll it
    would hide a successful run behind a crash exit. Returns False when the
    row was NOT re-armed: the caller turns that into exit 1, because a row
    left on its consumed event never fires again and nobody is watching."""
    try:
        return _rearm(watch, decision, submissions)
    except Exception as e:  # noqa: BLE001
        print(f"  re-arm FAILED for {watch.ticker}: {type(e).__name__}: {e} — the watch "
              f"still names the consumed event; re-`add` it.", file=sys.stderr)
        return False


def _completed(decision, rc: int) -> bool:
    """Did this event finish, so the row should be re-armed? A queued brief
    (5) is complete — the report and audit exist, the brief is retried on
    its own — a failed audit (4) is not."""
    return decision.action == "skip" or (
        decision.action in ("generate", "refuse") and rc in (0, BRIEF_PENDING_RC)
    )


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
        stamp = _utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
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
                        current = _find_watch(ticker)
                        if current is None:
                            print(f"  {ticker}: removed from the watchlist by a concurrent "
                                  f"run (sync --prune) — nothing to do.")
                            return 0
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
                        if not _rearm_guarded(watch, decision, submissions):
                            return max(rc, 1)
                    return rc

        if args.once:
            return 3  # "not yet" is not a failure; cron should not alert on it
        if time.monotonic() + args.interval > deadline:
            print(f"Gave up after {args.max_wait / 3600:.1f}h — no qualifying filing yet. "
                  f"Filing may be delayed; re-run or check EDGAR directly.", file=sys.stderr)
            return 1
        time.sleep(args.interval)


def _capture_vintage(ticker: str, client: SecClient, args: argparse.Namespace,
                     now: datetime) -> None:
    """Snapshot this name's companyfacts if it changed today.

    A company can revise a prior figure without re-presenting the original,
    leaving companyfacts holding only the new value — invisible from any single
    fetch. Only a snapshot taken BEFORE the revision can show it, and nothing
    can be back-filled, so this runs from every pass and costs one fetch a day
    per name. Best-effort by construction: the season does not stop because an
    archive write failed.
    """
    if args.dry_run or getattr(args, "no_vintage", False):
        return
    try:
        res = capture_vintage(client, ticker, now=now)
        if res.wrote:
            # Inside the guard: `_sweep_one` promises never to raise, and the
            # loop that calls it has no guard of its own, so a failed stat()
            # here would abort the pass for every name after this one.
            print(f"[{now:%Y-%m-%d %H:%M:%SZ}] {ticker}: companyfacts changed — "
                  f"vintage {res.path.name} ({res.path.stat().st_size / 1024:.0f} KB)")
    except Exception as e:  # noqa: BLE001 — an archive must never cost a print
        print(f"  {ticker}: vintage capture failed: {type(e).__name__}: {e}", file=sys.stderr)


def _sweep_one(client: SecClient, watch: wl.Watch, args: argparse.Namespace) -> int:
    """One pass for one watch: fetch, decide, act, re-arm. Never raises —
    a sweep must reach every name even when one of them fails."""
    now = _utcnow()
    stamp = now.strftime("%Y-%m-%d %H:%M:%SZ")
    pending = 0
    _capture_vintage(watch.ticker, client, args, now)
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
        # A queued brief is retried only while the 10-Q track is idle: when
        # this pass is about to generate, the audit hook rebuilds the brief
        # with the engine report and clears the queue itself — retrying first
        # would spend a headless run on a version overwritten minutes later.
        if not args.dry_run and not getattr(args, "no_brief", False):
            try:
                pending = _retry_pending_brief(watch.ticker)
            except Exception as e:  # noqa: BLE001 — the queue must not take the pass down
                print(f"[{stamp}] {watch.ticker}: queued brief retry crashed: "
                      f"{type(e).__name__}: {e} — still queued.", file=sys.stderr)
                pending = BRIEF_PENDING_RC
        overdue = (now - watch.print_at).days
        if overdue > OVERDUE_DAYS:
            # Not verbose-gated: a row whose expected period drifted out of
            # the match window looks exactly like patience, forever.
            print(f"[{stamp}] {watch.ticker}: still waiting {overdue}d past its print hint "
                  f"({watch.print_at:%Y-%m-%d}), expected period {watch.expected_report_date} "
                  f"— check the row (`status`) and re-`add` if the period is wrong.",
                  file=sys.stderr)
        elif args.verbose:
            print(f"[{stamp}] {decision.message}")
        # The 10-Q is not here yet — but the earnings 8-K may be.
        pending = pending or _print_night_guarded(watch, submissions, args, now)
        return pending or 3
    print(f"[{stamp}] {decision.action} — {decision.message}")
    try:
        rc = _act(watch.ticker, watch, decision, args)
    except Exception as e:  # noqa: BLE001
        print(f"  {watch.ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not args.dry_run and _completed(decision, rc):
        if not _rearm_guarded(watch, decision, submissions):
            return max(rc, 1)
    # Same pass as the 10-Q: the audit hook normally wrote the brief already,
    # in which case this is a no-op; if the audit failed (4) the print-night
    # brief still goes out — the release is news tonight, the audit can retry.
    pending = pending or _print_night_guarded(watch, submissions, args, now)
    return rc if rc not in (0, 3) else (pending or rc)


LOCK_RETRY_S = 0.5
OVERDUE_DAYS = 21  # a print hint this stale with no filing is a mis-armed row, not patience


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
    _notify_problems(worst, acted, args)
    return _worst([worst, *acted.values()])


_RC_WORDS = {1: "error", 2: "refused (no thesis)", 4: "audit FAILED", 5: "brief queued"}


def _notify_problems(sync_rc: int, acted: dict, args: argparse.Namespace) -> None:
    """One notification per pass that needs a human — never for a clean
    pass (a finished brief announces itself when it is written)."""
    if args.dry_run:
        return
    problems = [f"{t}: {_RC_WORDS.get(rc, rc)}" for t, rc in acted.items() if rc != 0]
    if sync_rc != 0:
        problems.insert(0, f"portfolio sync: {_RC_WORDS.get(sync_rc, sync_rc)}")
    if problems and not notify("FQE sweep needs attention", "; ".join(problems)):
        print("notification NOT delivered (osascript unavailable, FQE_NO_NOTIFY set, or no "
              "login session) — read this log: " + "; ".join(problems), file=sys.stderr)


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
    p_poll.add_argument("--no-vintage", action="store_true",
                        help="skip the daily companyfacts snapshot (data/vintages/)")
    p_poll.add_argument("--no-brief", action="store_true",
                        help="skip the one-page earnings brief after a successful audit")
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
    p_sw.add_argument("--no-brief", action="store_true",
                      help="skip the one-page earnings brief after a successful audit")
    p_sw.add_argument("--no-vintage", action="store_true",
                      help="skip the daily companyfacts snapshot (data/vintages/)")
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
