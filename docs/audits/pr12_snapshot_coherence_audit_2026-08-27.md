# PR #12 audit — snapshot coherence + scope disclosure (2026-08-27)

Independent adversarial audit of PR #12 ("Keep SEC report inputs coherent and
disclose analysis scope", branch `fix/nvda-report-reliability`, commit
`38beca8`), run as three parallel tracks: deep code audit, independent test
reproduction with stress probes, and domain research validating the PR's
premises against SEC EDGAR behavior.

## Verdict

**Mergeable. No confirmed bugs.** The single-fetch snapshot refactor is
correct, backward-compatible, mutation-safe, and closes a real defect: under
`--fresh`, a report previously performed three independent live Company Facts
fetches (fundamentals, documents, restatements) that could fail independently
or straddle an EDGAR update — meaning the restatement detector could analyze
a *different* snapshot than the one the mapper scored, violating the round-8
"same source the mapper reads" invariant. Post-PR there is exactly one fetch,
reused everywhere.

## Independent verification (reproduced, not trusted)

- `pytest tests/unit/test_edgar_adapter.py tests/unit/test_report_builder.py -q` → 8 passed (as claimed)
- `pytest -o addopts='' -q` → 494 passed, no warnings (as claimed); main → 491; delta is exactly the 3 new tests
- `python -m compileall -q app scripts tests` → clean
- 16 additional stress probes, all passing:
  - `build_dataset(snapshot.company_facts)` reproduces `snapshot.dataset` exactly (coherence)
  - `fetch_dataset()` output byte-identical to main's for the same fixture; both fetch exactly once
  - Aggressive mutation of the returned raw dict does not corrupt the disk cache or later fetches; no consumer (`edgar_documents`, `restatements`, offerings) mutates the shared payload
  - End-to-end render: scope notice appears exactly once, before the appendix, on both client and no-client paths; prefetched facts → zero restatement refetches; omitted facts → fallback still works
  - Real caller path (`journal.reporting.build_report`) offline against a seeded cache: report writes, notice present once, restatements checked from the snapshot

## Domain research — the PR's premises hold

- **Multi-fetch incoherence is real, not hypothetical.** SEC documents that the
  xbrl APIs update "throughout the day, in real time" (sub-minute processing);
  there is no cross-request consistency mechanism on data.sec.gov. Fetches
  minutes apart can straddle a dissemination, and EDGAR has had publicized
  dissemination incidents (Oct 2025). Fetch-once-reuse is the correct
  within-run pattern; the officially sanctioned cross-run coherent snapshot is
  the nightly `companyfacts.zip` bulk file (aligns with TARGET_ARCHITECTURE's
  vintage store).
- **The three disclosed risks are genuinely invisible to this engine's data
  path** — with one precision note: NVIDIA *does* XBRL-tag customer
  concentration (`ConcentrationRiskPercentage1`), but dimensionally, and
  companyfacts excludes dimensional facts (per the repo's own live-verified
  2026Q3 survey). Export-control exposure (H20/China) and purchase commitments
  are narrative/note disclosures. Accurate phrasing is "not captured by the
  non-dimensional companyfacts feed", not "absent from XBRL".
- **Explicit model-boundary disclosure is recognized practice** (SR 11-7 model
  risk management: documented limitations, misuse-by-ignoring-limitations).

## Findings (non-blocking)

- **F1 (doc, low)** — The PR description's "cannot mix independently fetched
  SEC snapshots" overclaims: a `--fresh` run still performs three independent
  live *submissions* fetches (documents / offerings / 8-K 4.02 events) that
  can diverge mid-run. Companyfacts is now coherent; submissions is not.
  Candidate follow-up: a `SubmissionsSnapshot` analogue.
- **F2 (wording, low)** — `ANALYSIS_SCOPE_NOTICE` enumerates exactly three
  unmodeled risks and could read as exhaustive; the engine equally does not
  model litigation reserves, related-party transactions, pensions, segments
  (`what_this_engine_can_and_cannot_do.md`). "Not analyzed (among others):" or
  "including:" would fix it. It is also a fourth disclaimer surface alongside
  §11 Disclaimer, the docs page, and legal framing.
- **F3 (cosmetic)** — The notice also renders on dataset-only paths
  (`app/api/routes.py`, `run_analysis.py`) where no filings were fetched, so
  "review the filing notes" refers to filings the run never touched.
- **Test gaps** — (1) nothing pins that the two entry points pass
  `snapshot.company_facts` into `fetch_documents` (a regression reinstating
  `client.company_facts(ticker)` there would pass the suite); (2) the
  `company_facts=` wiring from entry points into `build_report` is unpinned
  (journal tests stub `build_report`); (3) the scope notice's placement and
  uniqueness are unasserted (only substring checks); (4) no test pins legacy
  `fetch_dataset`'s 2-tuple contract.

## Call-site sweep (all clean)

`fetch_dataset` (2 script callers, 2-tuple intact) · `fetch_dataset_snapshot`
(journal reporting, generate_report, tests) · remaining `client.company_facts`
calls (fallback + out-of-report-path backtesting only) · `detect_restatements`
callers · both `build_report`s and their web/API/script/test callers ·
`fetch_documents` callers · report-markdown consumers (golden test uses
`render()` only — untouched; card-split assertions unaffected; web markdown
render OK). `calibration_snapshot.json` untouched; no scoring/anchor/PIT files
in the diff.
