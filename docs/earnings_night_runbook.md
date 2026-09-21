# Earnings-night runbook

Operating procedure for `scripts/watch.py`. Written for NVDA FQ2-27 on
**Wed 2026-08-26**, but the sequence is the same for any watched name.

## What is and isn't automated

Automated: the reminder before the print, watching EDGAR for the filing,
running the engine the moment it lands, the headless earnings-audit pass
over the generated report (`scripts/run_audit.py`, skippable with
`--no-audit`), and — with `sweep` — the calendar itself: each name is
re-armed for its next quarter the moment an event completes, and with
`--portfolio` every holding in `journal/portfolio.txt` is armed from its own
filing history.

Not automated, by design: the thesis, the AFTER block, the OUTCOME. Track 1 of
[evaluation_protocol.md](evaluation_protocol.md) is explicitly the track that
*requires the analyst*, and the entry is only evidence if the BEFORE block was
written blind ([JOURNAL.md](../journal/JOURNAL.md) rule 1).

**Two tracks, chosen by whether a thesis was locked before the print:**

- **Journal track** — a locked thesis exists: the poller shells to
  `journal.py report --fresh`, the report lands in `reports/`, and the entry
  becomes a blind case. Unchanged from the original design.
- **Ticker-only track** — no thesis on file: the poller does NOT write into
  `reports/`. It generates a clearly-bannered artifact in `reports/auto/`
  ("NOT journal evidence") and exits 0. `--no-auto` restores the strict
  behavior (refuse, exit 2) for anyone alerting on the gate.

The gate itself is untouched: a landed filing still never authorizes a
*journal* report — only a locked thesis does. A report in `reports/` before
you wrote a prior means no blind case is possible, and the season's lesson was
that the scarce resource is entries, not reports (16 reports, 1 entry,
0 outcomes). The auto track exists so that names you never intended to journal
still produce the full evidence artifact with zero input.

## NVDA timing (measured, not assumed)

From NVDA's EDGAR submissions history:

| Quarter | Earnings 8-K accepted | 10-Q accepted | Gap |
|---|---|---|---|
| FQ1-27 (2026-05-20) | 20:21:19Z | 20:35:52Z | 14 min |
| FQ3-26 (2025-11-19) | — | 21:36:17Z | (EST, same pattern) |

NVDA files the 10-Q **the same evening as the print**, roughly 15 minutes after
the release — unlike GOOGL, which made the engine wait. Expect for Aug 26 (EDT):

- ~**20:20Z** — earnings 8-K / press release
- ~**20:35Z** — 10-Q, which is what the engine needs

A 5-minute poll interval started at 20:15Z catches it inside ~5 minutes, so the
report should exist by ~20:45Z.

## The sequence

**1. Day before — check what needs a prior**

```
scripts/watch.py due --within-hours 36
```

Exit 1 means a watched name still needs a thesis. Exit 0 means you are ready.

**2. Before the print — lock the thesis (you, blind), then PIN it**

```
scripts/journal.py openv2 NVDA \
  --thesis "..." --conviction 3 --action hold \
  --assumption "revenue,>,57000000000,FQ3-27,,2026-11-25" \
  --catalyst "FQ3-27 print 2026-11-18"
scripts/watch.py link NVDA
```

`--thesis`, `--conviction` and `--action` are required; the lock additionally
refuses without at least one `--assumption` (the specificity floor). An
assumption is six comma-separated fields:

```
metric , comparator , threshold , window , source , resolve_by
```

**Leave `source` empty** (the doubled comma above) unless you intend to
resolve that row by hand. An assumption that names a source returns `pending`
forever until per-value provenance (P1-A) ships, because nothing can yet
attest the form and accession a value came from; a source-less row is a
numeric-only commitment and auto-terminates. That is the difference between a
row that scores itself and a row that sits open for a season.

Do this *before* 20:20Z. Afterwards the tape exists and the prior is no longer
blind — record it in `--contamination` if that happens.

`link` writes the entry's day and BEFORE-block hash onto the watch. Only that
exact, unmodified entry can authorize this event's report — a thesis from a
prior quarter (or an edited one) fails the gate instead of silently standing
in. No link, no journal-track report.

**3. At the print — start the poller**

```
EDGAR_IDENTITY="Your Name you@example.com" scripts/watch.py poll NVDA
```

Defaults: check every 5 minutes, give up after 6 hours. A filing counts when
it is a NEW accession beyond the baseline recorded at `add` time AND its report
period matches the event's expected fiscal period — the estimated print time
only schedules the polling, so a company that files *earlier* than estimated
still triggers (a forecast date is never a filing cutoff), and an intervening
earlier quarter's filing never hijacks the watch. Fetches use `fresh=True` — a
<24h cache can serve pre-filing data on exactly the night that matters (P0-D).

After generation the headless audit runs, and **`reported` is stamped only
when the audit succeeds** — a failed audit keeps the report on disk for
diagnosis, leaves the journal entry retryable, and exits nonzero so cron sees
the failure.

Exit codes: `0` report generated, audited, and marked · `2` filing landed but
no thesis (`--no-auto` only; act now) · `3` nothing yet (`--once` only) ·
`4` report generated but the audit FAILED (journal not marked; retry) ·
`5` case complete but the brief FAILED (queued; the next `sweep` retries it) ·
`7` the audit failed three times and was ABANDONED — the brief was built
without it and the row re-armed ·
`1` gave up, EDGAR failed, the watch has no event identity (re-`add` it), or
the row could not be re-armed after a completed case.

**4. After it generates — read, then fill AFTER**

```
scripts/journal.py after NVDA --impact changed_confidence --conviction-after 3 \
  --surfaced "..." --disagreed "..."
```

**5. Weeks later — the part that produces the actual evidence**

```
scripts/journal.py outcome NVDA
scripts/journal.py tally
```

## Rehearsing

Every time-dependent path takes an override so the schedule can be tested
before the night it matters:

```
scripts/watch.py due --within-hours 36 --now 2026-08-25T20:20:00Z
scripts/watch.py poll NVDA --once --dry-run --since 2026-05-20 --force
```

The second detects NVDA's real May 10-Q against live EDGAR and stops at the
gate — the full path with nothing written. `--force` is needed because the
May filing does not match the armed watch's expected report period: on an
armed watch, a `--since` match that disagrees with the event identity is
refused (exit 1) rather than allowed to consume the pinned entry for the
wrong event. That refusal itself is worth rehearsing once — run the same
command WITHOUT `--force` and confirm the mismatch error.

## Unattended operation — `sweep`

`poll` is one name, one night, foreground. `sweep` is the hands-off version:
one pass over the whole calendar, every name, then exit. Cron it and the only
per-season setup is keeping `journal/portfolio.txt` current (one ticker per
line, `#` comments; private and gitignored).

```
# crontab — hourly; 11 names is ~11 EDGAR requests per pass
0 * * * * cd /path/to/financial_quality_engine && \
  EDGAR_IDENTITY="Your Name you@example.com" \
  CLAUDE_BIN="$HOME/.local/bin/claude" \
  .venv/bin/python scripts/watch.py sweep --portfolio journal/portfolio.txt \
  >> journal/watch.log 2>&1
```

The scheduler's environment is not your shell's. Two things the audit and
brief need that cron does not provide:

- **The CLI.** `claude` is resolved as `CLAUDE_BIN`, then PATH, then
  `~/.local/bin/claude` (`app/services/headless.py`); cron's PATH is
  `/usr/bin:/bin`, so either set `CLAUDE_BIN` as above or accept the fallback.
- **A login.** Headless `claude -p` uses the CLI's own stored login, not the
  desktop app's. Before the season, run `claude` in a terminal and `/login`
  once, then prove it from a scheduler-shaped environment:

  ```
  env -i HOME="$HOME" USER="$USER" PATH=/usr/bin:/bin \
    "$HOME/.local/bin/claude" -p "Reply with exactly: OK"
  ```

  "Not logged in" or "OAuth session expired" there means every audit and
  brief of the season would fail (exit 4 on every pass) until you log in.
  On a Mac that sleeps, prefer a launchd LaunchAgent over cron: it runs in
  your login session and catches up after a missed hour; cron skips it.

Per pass, in order:

1. **Sync** (`--portfolio`): any holding not yet watched is armed exactly as
   `add` would — print hint from its 8-K 2.02 cadence, event identity from its
   periodic filings. A name that cannot be armed (too little history) is
   reported and skipped; the pass continues. Names watched but no longer held
   are listed, and removed only with `--prune` — never while a thesis is
   pinned to them (that is an event in flight).
2. **Detect** — every armed watch is checked against fresh EDGAR submissions
   with the same event-identified `decide()` as `poll`. Nothing about the
   forecast date is a cutoff: an early filing triggers on the next pass.
3. **Act** — a landed filing takes the journal track if its thesis is pinned
   and locked, the bannered `reports/auto/` track otherwise; the audit runs
   on both (same exit codes as `poll`), and after a successful audit the
   one-page **earnings brief** is written (`scripts/earnings_brief.py`; see
   below). A failed brief does not un-complete the case (the row is still
   re-armed) but it is not forgotten: the name exits 5, the failure is queued
   under `reports/briefs/.pending/`, and every later pass retries it first —
   so a season-long fault (no CLI on the scheduler's PATH, an expired login)
   shows up as a run of 5s, never as a quiet season without briefs.
4. **Re-arm** — once an event completes (exit 0 on either track, or a skip),
   the row is rewritten for the NEXT quarter from the issuer's history: new
   baseline accession (the filing just consumed), next expected period, next
   print hint, pin and label cleared, and a `note` recording the derivation.
   A failed audit (exit 4) is *not* re-armed, so the next pass retries the
   same filing — but only three times. Each retry is a paid headless run and
   a deterministic failure never improves by repeating, so after the third
   the audit is ABANDONED: the brief is built without it (engine findings go
   in uncorrected), the case completes, the row re-arms, and the pass exits
   7 naming the company. Re-arming is the point — while the row is not
   re-armed, every hourly pass rebuilds the report and spends another run.
   A name therefore never has to be `add`ed twice. If the
   re-arm itself fails (the row could not be rewritten), the case is still
   complete but the name exits 1 and says `re-arm FAILED` — that row still
   names the consumed event and will never fire again until you `add` it.

Exit code is the worst per-name code, except that "still waiting" (3) is 0
and a sweep that finds another sweep already running (an audit can outlast an
hourly interval; `journal/sweep.lock`) yields with 0. Names still waiting are
silent unless `--verbose` — with one exception: a name still waiting more
than 21 days past its print hint is named on stderr on every pass, because a
row whose expected period drifted out of the ±21-day match window looks
exactly like patience. Check it with `status`; if the period is wrong,
`add` it again.

What this does NOT change: the blind thesis. `sweep` never writes into
`reports/` without a pinned, lock-verified entry — a thesis-less print is the
auto track, exactly as under `poll`. If you want a journal case for a name,
`journal.py openv2` + `watch.py link` before the print is still the step
that only you can take; `due` still tells you when it is time.

Rehearse without side effects (both write nothing — `--dry-run` covers the
sync step too):

```
scripts/watch.py sweep --dry-run --verbose --portfolio journal/portfolio.txt
scripts/watch.py sync --dry-run             # what it would arm / remove
```

`poll` and `sweep` share one activity lock (`journal/sweep.lock`): a manual
`poll` started during the cron window waits for a running sweep to finish
(at most the rest of its own `--max-wait`, then exit 1), re-checks the row
and EDGAR, and simply exits 0 if the sweep already consumed the filing —
never a second generate+audit of the same print. A sweep holds the lock for
its whole pass, so during a multi-name earnings night a manual `poll` may
wait for several audits.

Check `journal/watch.log` afterwards. Exit 2 in that log means `--no-auto`
was set and a filing landed with no thesis on file.

## Print night vs the 10-Q, and how you hear about it

The engine report needs the quarter's XBRL facts, so the report/audit track
fires when the **10-Q/10-K** lands. The brief is a read of the print itself,
so `sweep` also fires a **brief-only pass on the earnings 8-K** (Item 2.02)
the hour it appears: release plus call transcript, engine findings marked
UNAVAILABLE. When the 10-Q lands the same brief file is rebuilt with the
engine findings (your `useful:` value carries over). For NVDA the two are
minutes apart; for a small cap the 10-Q can be weeks later. The brief's
filename (`<TICKER>_<8-K date>.md`) is the only state: no brief for that
date within 14 days of the 8-K means build one. A failed print-night brief
is queued and retried like any other (exit 5) — but never in the pass that
is about to rebuild it with the engine report anyway, and at most six times
(the 10-Q rebuild is its second chance; an hourly paid run for weeks is
not). Amended 8-Ks (8-K/A) never count as the print, so a corrected exhibit
days later cannot move the brief to a second file; the prior-quarter guide
must be at least 45 days older than the print, so a preliminary-results
8-K is never mistaken for last quarter's release. Two earnings 8-Ks within
the window (Boeing's preliminary-then-final pattern) each get a brief; if
they share a day, the print-night brief is rebuilt from the newer one.

Each brief records how it was built in `reports/briefs/<T>/<date>/built.json`
(`kind`: `print-night` or `full`, the 8-K accession, the report path). A
print-night build never downgrades a brief that already carries the engine
findings — a queued retry or a stray `--no-report` by hand is a no-op then.

Two deliberate limits: `poll` is the 10-Q track only (on print night, run
`earnings_brief.py build TICKER --no-report` by hand if you are at the
keyboard), and `--no-auto` means nothing unattended for a thesis-less name,
the brief included.

Delivery is local and publishes nothing (the repo is public; `reports/` is
not in it):

- **Notification.** A macOS notification when a brief is written (title is
  the ticker, body is the brief's headline), and one per pass when
  something needs you (an audit failed, a brief is queued, a name refused).
  A clean pass is silent. `FQE_NO_NOTIFY=1` turns notifications off.
- **Drop folder.** Every finished brief is copied to
  `iCloud Drive/Earnings Briefs/` — readable in Files on your phone — or to
  `FQE_BRIEF_DROP=<dir>` (use an absolute path) if set. Rebuilds overwrite,
  so the phone shows the latest version. `earnings_brief.py build
  --no-deliver` skips both. "Copied to" means written to that folder on this
  Mac; whether iCloud syncs it is iCloud's business — if you are signed
  out, the copy sits here. A notification that could not be posted, and a
  copy that failed, are both said so in the log.

A print-night brief by hand: `scripts/earnings_brief.py build NVDA --no-report`.

## The vintage store — what the numbers used to say

A company can revise a prior figure and simply not re-present the original.
Companyfacts then holds only the new value, and the change is invisible from
any single fetch, forever. The engine already catches the loud case (two
filings both presenting the same period); the quiet one can only be caught by
having kept what the number used to be.

So every sweep pass (and every `poll`) snapshots each watched name's
companyfacts once a day, into `data/vintages/CIK…/<date>-<hash>.json.gz`
(gitignored; about 0.3 MB a snapshot, named for its content so two documents
on one day are two files and identical content is stored once). Companyfacts
itself cannot be rewound: it is a live view, and a value it no longer carries
cannot be asked for. An as-filed history may be reconstructible from SEC
DERA's quarterly data sets, but that is a separate project at quarterly
granularity — not a substitute for a daily snapshot taken now. That is why
capture runs before anything reads from it.

```
scripts/vintage.py capture NVDA        # by hand; the sweep does this daily
scripts/vintage.py list NVDA
scripts/vintage.py diff NVDA           # newest two snapshots
scripts/vintage.py diff NVDA --from 2026-09-19 --to 2026-12-01 --since 2025-01-01
```

A diff reports two things about the figures the engine actually scores: one
whose value **changed**, and one that **disappeared**. New periods are not
reported — an ordinary filing adds those. A field the filer moved to another
XBRL tag is compared, not called a disappearance, and the new tag is named.
Share counts are excluded unless you pass `--splits`: a stock split
retroactively rewrites every prior share count, and on the first real capture
NVDA's ten-for-one split was the only thing the diff found. `--no-vintage`
turns capture off. A capture that fails for two days running makes the pass
A row still waiting more than 21 days past its print hint is named on stderr
on every pass **and notified once a day**: that is the only signal that a
name has silently dropped out of the season (a drifted expected period looks
exactly like patience, forever). It stays exit 3 — waiting is not an error —
so the notification is the whole alert.

exit 6 and names the company in the notification — ranked below every
print-related code, because a print that did not complete is more urgent,
but not silent, because this is the one thing that cannot be back-filled.

**Read a finding as context, not an alarm.** Nothing the diff can see has an
amended filing behind it — that is exactly what makes it invisible to the
report's own restatement section, and it also means the ordinary explanations
come first: a discontinued operation or spinoff re-presented, a segment
reclassification, a taxonomy migration. At the published base rate for
genuine restatements, eleven holdings should produce roughly one true finding
every year or two, and a handful of benign ones along the way. The store
earns its keep on the quarter that is not benign, which is exactly the
quarter you cannot reconstruct afterwards.

## Standing assumptions — the thesis without the journal

A brief with no thesis has nothing to measure against, and a blind thesis
per print is the thing there was never time for. The middle path: write
down, once per holding, the two or three things you are assuming, and every
brief reports each one as **held / challenged / no news** with the fact and
the file that decided it.

```
scripts/earnings_brief.py assume NVDA "Data-center revenue keeps growing >50% YoY"
scripts/earnings_brief.py assume NVDA "Buybacks at least offset SBC dilution"
scripts/earnings_brief.py assume NVDA            # list them
```

That writes `journal/assumptions/NVDA.md` (private, gitignored — it reveals
holdings; hand-editing the bullets is fine). Retire an assumption by
deleting its line. The brief's `## Your assumptions` section is the second
thing on the page; the digest carries it too. A verdict is only ever
`held` or `challenged` when a supplied document states the fact — otherwise
`no news` — so a run of `no news` across a season is itself information:
the assumption is not something the prints can test.

### When you have written none

An empty `journal/assumptions/` used to turn that section into one
`UNAVAILABLE` line on every brief, for every holding, all season. It no
longer does. With no file on hand the engine derives the assumptions from
the company's own filed quarterly history instead — the continuity claims
the last two years actually support, one per dimension it can speak to:

```
scripts/earnings_brief.py assume AMKR --derive    # preview; writes nothing
```

```
1. Revenue keeps growing year over year — it has in each of the last four quarters, by 3.4% to 27.5%.
2. Gross margin stays at or above 12.0% — its low over the last four quarters (FY2025Q2).
3. Shares outstanding grow no more than 0.3% year over year — its fastest over the last four quarters (FY2026Q1, the most recent).
4. Total debt stays at or below $1.98B — its high over the last four quarter ends (FY2025Q3).
```

These are the filings' recent past, not a thesis and not a forecast, and the
brief says so above the table (`_Derived from this company's filed history —
not your own assumptions._`). `challenged` then means this print broke a
pattern that had held — on AMKR's July print, row 4 above: total debt went
from $1.4B at March 31 to $2.5B at June 30.

Rules decline rather than guess. A series that is erratic, too short, missing
from the filings, or moved in one step produces no claim at all, so a name may
derive three assumptions where another derives five — and a dual-class filer
that tags no single share count derives none for dilution. The one-step test
covers stock splits and large one-off raises alike, on the same ground: neither
is a rate, so no "grows no more than X% a year" sentence describes it. That
costs real coverage — Boeing's 2024 raise moved its share count 21% in a
quarter, so BA derives no dilution claim at all — and a lost row is the cheaper
error than a fabricated one.

Two things to keep in mind when reading a season of them. A bound set by the
quarter that just printed has never been tested, so those claims are phrased
differently on purpose ("does not fall further — it just set a four-quarter
low of 67.2%") rather than as a floor that has held. And a rule only fires when
its trailing window is already clean, which tilts the *set of claims that exist
at all* toward ones likely to keep holding: a season reading mostly `held` is
partly an artifact of which claims were allowed to be made, not proof on its
own that nothing moved.
Your own assumptions always win: write one and the derived set stops being
used for that holding. `--derive` never writes to your file, because once a
derived claim lands in `journal/assumptions/` nothing downstream can tell it
from something you actually believe.

## The earnings brief

`reports/briefs/<TICKER>_<print date>.md` — one page per print, written by
one headless Claude run over primary sources that the script collects
first and keeps beside the brief (`reports/briefs/<TICKER>/<date>/`):

| Source | Where it comes from |
|---|---|
| Earnings release | The 8-K Item 2.02 **EX-99.1**, selected by EDGAR exhibit type (never by filename — NVDA calls it `q2fy27pr.htm`) |
| Prepared remarks / CFO commentary | Further narrative EX-99 exhibits, when the filer attaches them (NVDA: EX-99.2) |
| Call transcript | **Not on EDGAR.** Drop a text file at `journal/transcripts/<TICKER>/<print date>.txt` (private, gitignored) or pass `--transcript`; until then the call section reads `UNAVAILABLE` |
| Engine report + audit | The report the sweep just generated and its `_audit.md` |
| Last quarter's brief | For the "changed since last quarter" section |

Fixed headings (`.claude/skills/earnings-brief/SKILL.md`): headline · a
five-dimensional quarter assessment · standing assumptions · results vs the
company's own prior guide · guidance · KPIs and segments · management framing ·
the call (prepared remarks, then every question with
answered/partial/deflected) · engine findings worth carrying · changed since
last quarter · open questions · sources. Numbers only from the supplied
files; anything missing is marked, not filled in.

The quarter assessment says whether the reported results, forward guidance,
operating KPIs, cash/earnings quality, and balance-sheet/capital evidence were
favorable, mixed, unfavorable, or not assessable. It is validated before the
brief is written and also saved as `reports/briefs/<TICKER>/<date>/assessment.json`
for a later UI/API. It deliberately does not rate the investment: price,
valuation, market expectations, and required return are a separate plane.

```
scripts/earnings_brief.py build NVDA                 # re-run any time; e.g. once the transcript exists
scripts/earnings_brief.py build NVDA --transcript ~/Downloads/nvda_call.txt
scripts/earnings_brief.py digest --since 2026-10-01  # one page across the season (deterministic)
scripts/earnings_brief.py tally                      # useful: yes/no counts
```

Every brief ends in `useful: unset`. Flip it to `yes` or `no` after reading;
the tally over a season is the low-friction replacement for the journal's
"did this change anything" question when no blind thesis was written.
Regenerating a brief keeps a value already set.
