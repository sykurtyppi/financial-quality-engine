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
| 1 | Baseline report | Exits 0. Report and evidence ledger written, vintage captured. |
| 2 | Rerun, same inputs | Step 1's report is **moved to `reports/archive/`**, not overwritten. The new report is byte-identical once the `Data fetched:` and `Vintage snapshot:` lines are masked, and so is the ledger once `fetched_at` is masked. |
| 3 | A 10-Q/A lands | The scored revenue fact for the newest directly reported quarter is re-filed +10% as a 10-Q/A filed today, and listed in the filing index. The card has `Restatement (10-Q/A) affecting <quarter>`, and **no** `Silent revision:` line. The ledger cites the /A accession. The silent-revision check compares two snapshots and attributes the move to the /A. Step 2's report is archived. |
| 4 | Same-day conflicting facts | A second value for that fact, same day and form. A field note names the conflict, and it is not called a restatement. |
| 5 | Cash-flow statement missing | Every CFO concept is removed. `Critical field 'cfo' missing`, and the card is marked incomplete. |
| 6 | One period of history | **Exit 2**, `error: …` then `no report written`, no traceback, no file. |
| 7 | SEC down except companyfacts | Filing index unavailable. Its streams say UNAVAILABLE, never "clean". |
| 8 | Stale cache / `--fresh`, SEC unreachable | A 25 h-old companyfacts entry, and `--fresh`, both exit 2 with `SEC request failed`. No report is written. |
| 9 | Journal path | `openv2`, then `report --no-fresh --no-docs`. A second `report` is refused. `after --disagreed` records the analyst override, and `verify` still passes. |
| 10 | Rollback | Step 1's run is restored from the archive: report and ledger byte-identical to step 1. The vintage store is append-only (step 1's snapshots are unchanged, and the /A's snapshot is kept). |

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
  pair them. Now the earlier report, its ledger and its audit move to
  `reports/archive/<T>_<day>.<HHMMSS>.md` (`.ledger.json`, `_audit.md`)
  first. An archived run keeps its own companions by name, and the archive
  never overwrites.
- **A payload that cannot be mapped crashed.** Too little history raised a
  `ValueError` traceback (exit 1). Now `generate_report.py` prints
  `error: <T>: …` and `no report written: …`, and exits 2, the same
  contract as an acquisition failure.
- **A replay could be read as the latest report.**
  `earnings_brief.latest_report` and `watch._latest_report` picked the newest
  file by mtime, and a `.replay.md` rebuilt today is newest. Replays are now
  excluded, as audits already were.

Not changed: there is no clock pin. `fetched_at` and `Data fetched:` carry
the wall clock, so the determinism check masks exactly those lines and the
ledger's `fetched_at`. Everything else must match byte for byte.

## Restoring an earlier run

```
ls reports/archive/NVDA_2026-11-18.*           # pick the run: .<HHMMSS>.md
# set the current run (report, ledger, audit) aside, archived like a rerun would:
.venv/bin/python -c "from pathlib import Path; from app.services.reporting.report_files \
import archive_existing as a; print(a(Path('reports/NVDA_2026-11-18.md')))"
cp reports/archive/NVDA_2026-11-18.210507.md          reports/NVDA_2026-11-18.md
cp reports/archive/NVDA_2026-11-18.210507.ledger.json reports/NVDA_2026-11-18.ledger.json
```

Copy the archived `_audit.md` back too if the run had one. Step 10 does
exactly this and checks the restored report and ledger byte for byte. The vintage store is never rolled
back: it is the record of what SEC served, and a rollback of the report does
not change that.

## The operator's part

After a PASS, open `drill_log.md` and fill in **Operator notes**:

1. Read step 1's report and step 3's report end to end, from
   `work/night/reports/archive/` (every run of the night is there), and
   compare the card lines to the scenario each step says it applied.
2. Note anything slow, surprising, or worded so that it could be misread.
3. Record the decision, ready for the season or not, and why.

Run it on the fixture after every change to report generation. Run it on
`--cache` for one real name per sector before a season. A drill on real
inputs is what finding 3 asks for. The fixture run only keeps the machinery
honest.
