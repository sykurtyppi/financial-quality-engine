# Evaluation Protocol (opened 2026-07-03)

## Config freeze

**Scoring config 0.3.0 is FROZEN for the duration of this evaluation window.**
No weight, anchor, threshold, or detector changes until the window closes —
tuning during measurement fits the eval and voids the results. The freeze is
enforced socially by this document and mechanically by
`tests/integration/test_calibration_reproducibility.py` (any config change
breaks the snapshot and must cite this protocol in the diff).

Bug fixes that change *computation correctness* (not scoring judgment) are
allowed but must be logged in the "Mid-window changes" section below.

Mid-window changes: (none yet)

## The three evaluation tracks

### Track 1 — Research journal (dogfooding; requires the analyst)

The only question that matters: **does the output change what you'd do?**

Protocol per name (use names you actually know or follow):

1. BEFORE generating anything, write 2-3 sentences: your current view and
   what you believe about its accounting/earnings quality.
2. Generate the full report: `scripts/generate_report.py TICKER`
3. Record in `journal/JOURNAL.md`:
   - **Caught**: anything surfaced you did not know
   - **Benign-flagged**: anything flagged that you know is fine (say why —
     these feed Track 3 adjudication)
   - **Missed**: anything you know matters that the report is silent on
   - **Verdict**: changed my view / sharpened questions / no effect
4. Do at least 15 names over the window. Do NOT fix the engine mid-window;
   log irritations in the journal instead.

### Track 2 — False-negative miss test (automated)

Score the pre-trouble quarters of known accounting/quality blowups that
still file with the SEC, using point-in-time data. A screen that is quiet on
known cases is worse than one that is noisy on clean ones. Results:
`docs/false_negative_check.md`. Survivorship caveat: delisted blowups cannot
be tested with free data.

### Track 3 — Wide sweep + flag adjudication (automated + analyst)

Fundamentals-only scoring across the largest N SEC filers
(`scripts/wide_sweep.py`), then MANUAL adjudication of every flag in
`data/sweep/adjudication.csv` using this taxonomy (one label per flag):

| Label | Meaning |
|---|---|
| `genuine_concern` | the flag is right; the item deserves analyst attention |
| `industry_normal` | metric is normal for this business model/sector |
| `model_artifact` | anchors/formula misread the situation (note how) |
| `data_artifact` | ingestion gap or mapping issue drove the flag |

Rows with coverage below 60% are pre-labeled `data_artifact?` automatically —
confirm or overrule. Adjudication labels are the training sample for the
deferred archetype-anchor work (v0.5): do not spend them early.

## Window close

At close: tally journal verdicts, adjudication label distribution, and miss-
test results. Only then change the config (one reviewed change set +
snapshot regeneration), and log the changes in calibration_report.md v2.
