# Earnings-night runbook

Operating procedure for `scripts/watch.py`. Written for NVDA FQ2-27 on
**Wed 2026-08-26**, but the sequence is the same for any watched name.

## What is and isn't automated

Automated: the reminder before the print, watching EDGAR for the filing,
running the engine the moment it lands, and the headless earnings-audit pass
over the generated report (`scripts/run_audit.py`, skippable with
`--no-audit`).

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
  --thesis "..." --conviction 3 \
  --assumption "..." --catalyst "FQ2-27 print 2026-08-26"
scripts/watch.py link NVDA
```

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
`1` gave up, EDGAR failed, or the watch has no event identity (re-`add` it).

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

## Unattended operation

`poll` is a foreground process by design: it ends by generating a report you are
expected to read. If you want it started automatically, schedule the *start*,
not the reading:

```
# crontab — 20:15Z on 2026-08-26 (adjust TZ for your box)
15 20 26 8 * cd /path/to/financial_quality_engine && \
  EDGAR_IDENTITY="Your Name you@example.com" \
  .venv/bin/python scripts/watch.py poll NVDA >> journal/watch.log 2>&1
```

Check `journal/watch.log` afterwards. Exit 2 in that log means the filing landed
and nothing was generated because no thesis was on file.
