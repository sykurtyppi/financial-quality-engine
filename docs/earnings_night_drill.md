# Earnings-night drill

A rehearsal of a filing night, run before a season and after any change to
report generation. `scripts/drill.py` runs the operator's own commands
(`generate_report.py`, `journal.py`) against a scratch copy of the code and an
SEC cache seeded from its inputs, then breaks the inputs the way a filing
night does, and writes a log to sign. It answers Hermes audit round 7,
finding 3: "prove it on an earnings night: acquisition failure, stale EDGAR
data, amendment arrival, conflicting tags, partial statements, rerun
determinism, report generation, analyst override, rollback".

A PASS means **the commands behave** on these inputs: right exit codes, no
tracebacks, the right lines present or absent, nothing overwritten. It does
not mean the reports are right. Reading them is the operator's part of the
drill (below).

## Running it

```
.venv/bin/python scripts/drill.py                       # bundled AAPL fixture
.venv/bin/python scripts/drill.py --ticker NVDA --cache data/cache
.venv/bin/python scripts/drill.py --only 6,7,8          # a subset
```

- **Default inputs:** `tests/fixtures/real/companyfacts_AAPL_trimmed.json`
  (real AAPL facts, trimmed) plus a synthetic filing index: one 10-Q and one
  8-K Item 4.02. CI runs this (`cli-drills`).
- **`--cache DIR`:** a real SEC cache, normally `data/cache` after a
  `generate_report.py <T>` run on a machine that can reach SEC. The drill
  copies it and treats every entry as fetched just now. Run a real report for
  the name first, so the cache holds its companyfacts, filing index and
  documents.
- **Offline, and isolated.** Proxies point at a closed port, so nothing
  reaches SEC. Every run happens in `drills/<UTC stamp>/work/`, and the real
  `reports/`, `data/` and `journal/` are never touched (the drill's test
  checks this).
- **Output:** `drills/<stamp>/drill_log.md` (to read and sign),
  `drill_log.json`, and each step's command output under `steps/`. The
  reports each step wrote stay under `work/<workspace>/reports/`. The exit
  code is 0 only if every check passed.

## The steps

Steps 1–3 and 10 are one ticker's night, run in order in one workspace. The
others each start from a clean copy of the inputs.

| # | Step | What must happen |
|---|---|---|
| 1 | Baseline report | Exits 0. Report and evidence ledger written, vintage captured; the report names the engine commit that built it. |
| 2 | Rerun, same inputs | Step 1's run is **kept whole in its own generation** (`reports/.generations/<T>_<day>/`), not overwritten. The new report is byte-identical once the `Data fetched:` and `Vintage snapshot:` lines are masked, and so is the ledger once `fetched_at` is masked. |
| 3 | A 10-Q/A lands | The scored revenue fact for the newest directly reported quarter is re-filed +10% as a 10-Q/A filed today, and listed in the filing index. The card has `Restatement (10-Q/A) affecting <quarter>`, and **no** `Silent revision:` line. Every card line whose metric read the amended revenue carries `⚠ reads a revised figure: revenue <quarter> <was> → <now> (amended by 10-Q/A <accn>)`, and lines that do not read it (accruals, CFO / net income) carry none. The ledger cites the /A accession. The silent-revision check compares two snapshots and attributes the move to the /A. Step 2's report is archived. |
| 4 | Same-day conflicting facts | A second value for that fact, same day and form. A field note names the conflict, and it is not called a restatement. |
| 5 | Cash-flow statement missing | Every CFO concept is removed. `Critical field 'cfo' missing`, and the card is marked incomplete. |
| 6 | One period of history | **Exit 2**, `error: …` then `no report written`, no traceback, no file. |
| 7 | SEC down except companyfacts | Filing index unavailable. Its streams say UNAVAILABLE, never "clean". |
| 8 | Stale cache / `--fresh`, SEC unreachable | A 25 h-old companyfacts entry, and `--fresh`, both exit 2 with `SEC request failed`. No report is written. |
| 9 | Journal path | `openv2`, then `report --no-fresh --no-docs`. A second `report` is refused. `after --disagreed` records the analyst override, and `verify` still passes. |
| 10 | Rollback | Step 1's generation is made live again in one step (`restore`): report and ledger byte-identical to step 1, and step 3's run still kept whole. The vintage store is append-only (step 1's snapshots are unchanged, and the /A's snapshot is kept). |
| 11 | A rebuild that fails | After a good report, a rerun whose report build raises once the data is in hand (injected by the drill's shim, `FQE_DRILL_FAIL_BUILD=1`, in the workspace's copy only). It exits nonzero, and the live report and ledger are **byte-identical** to before. No generation is added, and no staged file is left. The next rerun succeeds and keeps the first run as the earlier generation. |
| 12 | A ledger that cannot be built | After a good report, a rerun whose evidence ledger build raises (`FQE_DRILL_FAIL_LEDGER=1`, in the workspace's copy only). It exits nonzero and says no report was published. The live report and ledger are **byte-identical** to before, no generation is added, and no staged file is left. Before the fix, the report went live without its ledger and the earlier complete run was archived. |

### Known issues

A check marked `[!]` in the log is a defect the drill found. It has been
reported but is not fixed yet. While it reproduces it does not fail its step.
Once it stops reproducing, the step **fails**, so the fix must remove the
marker (`KNOWN_*` in `scripts/drill.py`). Current: **none**.

Fixed:

- **An amendment was also reported as a silent revision** (step 3, found
  2026-09-26). The vintage diff compared scored values between snapshots
  without asking whether a new filing explained a move. So the 10-Q/A
  produced both `Restatement (10-Q/A) …` and a Tier-1 `Silent revision:
  revenue …` line, and the appendix listed the row under "Nothing here has an
  amended filing behind it". Now a move is *explained by a filing* when the
  newer snapshot still carries the original fact (same accession, period and
  value) beside the later filing the value now comes from
  (`VintageChange.explained_by_filing`). Such a move is never promoted. It is
  listed apart in the appendix ("Moved with a later filing (not silent)",
  with the filing that revised it), and counted apart on the status line
  (`0 change(s) (+1 moved with a later filing, not silent)`). A value
  changed under the same accession, or a new filing whose original is gone
  from the newer snapshot, stays silent and is promoted as before.
  Limit: a derived quarter whose move comes from an amended fact ending on
  another date is not explained this way. For example, Q4 = FY − 9M moved by
  a 10-Q/A to the nine-month figure: the change is attributed to the FY fact,
  which did not change, so it still reads as silent. The restatement scan's
  "Derived quarters that moved" section is where that one is explained.

## What the drill changed in report generation

Found while the drill was being built. Fixed alongside it:

- **Rollback existed only in version control.** A same-day rerun of
  `generate_report.py` (or `journal.py report` / the auto track) replaced
  the earlier report with no copy. It also left the earlier run's
  `_audit.md` beside the new report, where `earnings_brief.audit_for` would
  pair them. Now every run is a generation, kept whole: the rerun is built
  in `reports/.staging/<id>/`, and only once its report and ledger exist is
  it moved, whole, to `reports/.generations/<T>_<day>/<stamp>_<seq>_<id>/`. A build
  that fails leaves the live report exactly as it was; step 11 checks this.
  Earlier generations are never overwritten or moved: they are the archive.
- **A run is published whole, or not at all** (Hermes deep audit, findings
  1-2, and the re-audit, F1-F3). A ledger that could not be built used to be
  logged, and the report went live without it; the rebuild now fails
  (`generate_report.py` exits 3, "no report published") and the earlier run
  stays live, which step 12 checks. Every publish stamps the report (its
  last line, `- Generation: <id>`) and the ledger (`generation_id`) with one
  id. The live names (`<T>_<day>.md`, `.ledger.json`, `_audit.md`) are fixed
  symlinks through ONE pointer, `.generations/<T>_<day>/current`, and a
  publish is a single atomic swap of that pointer, after the generation is
  complete and fsynced: a publisher killed at any point (not only one that
  raises) leaves the earlier run live or the new one, never one's report
  beside the other's ledger. The re-audit found the version before this
  replaced the ledger and the report in two steps, and a process killed
  between them split them. Publishes of one report are serialized by a lock
  (`reports/.staging/<base>.lock`); readers take none. `read_live` pins one
  generation and returns its own paths, which no publish changes, so
  `earnings_brief` hands its model the pinned report and audit, and
  `run_audit.py` audits the pinned report and writes the audit into that
  generation: a report rebuilt mid-audit never gets the earlier run's audit.
- **A payload that cannot be mapped crashed.** Too little history raised a
  `ValueError` traceback (exit 1). Now `generate_report.py` prints
  `error: <T>: …` and `no report written: …`, and exits 2, the same
  contract as an acquisition failure.
- **A replay could be read as the latest report.**
  `earnings_brief.latest_report` and `watch._latest_report` picked the newest
  file by mtime, and a `.replay.md` rebuilt today is newest. Replays are now
  excluded, as audits already were.

Not changed: there is no clock pin. `fetched_at` and `Data fetched:` carry
the wall clock, and every run is its own generation, so the determinism
check masks exactly those lines, the `- Generation:` line and the ledger's
`fetched_at` and `generation_id`. Everything else must match byte for byte.

## Restoring an earlier run

```
ls reports/.generations/NVDA_2026-11-18/      # the runs, in publish order: <stamp>_<seq>_<id>
.venv/bin/python -c "from pathlib import Path; from app.services.reporting.report_files \
import restore; print(restore(Path('reports/NVDA_2026-11-18.md'), '20261118T210507Z'))"
```

`restore` takes the generation's directory name or any unique part of it
(its stamp, its sequence number or its id), and makes it live in one step: the report, ledger and
audit (if it had one) all switch together, and the run it replaces stays
kept. `set_aside` takes the live run off the live names without putting
another in its place. Step 10 restores step 1's generation and checks the
report and ledger byte for byte. The vintage store is never rolled back: it
is the record of what SEC served, and a rollback of the report does not
change that.

Live files written before generations (plain files at the live names) are
kept as a generation of their own by the first rebuild after this change. A
plain file that is not the live generation's (a file copied back by hand)
stops the rebuild (and a `restore`), which says so, rather than being
overwritten. Published files are read-only, so a hand `cp` over a live
name fails instead of editing a kept run. Copy a reports directory with
`cp -a` (links kept): a copy that dereferences the links is refused with
an error saying so.

## The operator's part

After a PASS, open `drill_log.md` and fill in **Operator notes**:

1. Read step 1's report and step 3's report end to end, from
   `work/night/reports/.generations/` (every run of the night is there), and
   compare the card lines to the scenario each step says it applied.
2. Note anything slow, surprising, or worded so that it could be misread.
3. Record the decision, ready for the season or not, and why.

Run it on the fixture after every change to report generation. Run it on
`--cache` for one real name per sector before a season. A drill on real
inputs is what finding 3 asks for. The fixture run only keeps the machinery
honest.
