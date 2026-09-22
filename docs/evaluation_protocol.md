# Evaluation Protocol (opened 2026-07-03 · 0.3.0 window CLOSED 2026-08-02)

## Window closure and rebaseline (2026-08-02)

**The 0.3.0 evaluation window is formally closed and rebaselined at config
0.4.0.** The 2026-08 first-principles reassessment
([PROJECT_STATE_ASSESSMENT.md](PROJECT_STATE_ASSESSMENT.md),
[ROADMAP_2026Q3.md](ROADMAP_2026Q3.md)) found correctness defects that made
frozen-config measurement meaningless to continue: quarterly/annual basis
mismatches (P0-A/B) put several scored values on the wrong scale, and
detectors with a measured 100% live false-positive rate were still scoring.
Freezing a config whose outputs are known-wrong does not protect an
evaluation — it voids it in the other direction.

What the closure means:

- Scores produced under 0.3.0 (the 2026Q2 season reports, the calibration
  report, the wide sweep) are **not comparable** to 0.4.0 scores. They remain
  valid as records of what the engine said at the time.
- Track 1 (journal) continues unchanged under 0.4.0 — its unit of account is
  decisions, not scores; the one locked case (MXL) predates the change and
  is 0.3.0-era.
- Track 3's 0/29 unadjudicated flags were generated under 0.3.0; if
  adjudicated, results describe 0.3.0 behavior only.
- 0.4.0 is now the frozen baseline under the same rule below, with one
  pre-declared exception: the YoY spread anchors are judgment-based pending
  reference-class distributions (roadmap P2-E); replacing them with empirical
  percentiles is a scheduled change, not mid-window tuning.

## Config freeze (now applies to 0.4.0)

No weight, anchor, threshold, or detector changes until the window closes —
tuning during measurement fits the eval and voids the results. The freeze is
enforced socially by this document and mechanically by
`tests/integration/test_calibration_reproducibility.py` (any config change
breaks the snapshot and must cite this protocol in the diff).

Bug fixes that change *computation correctness* (not scoring judgment) are
allowed but must be logged in the "Mid-window changes" section below.

Mid-window changes (0.4.0 window):

1. **2026-09-21 — per-block Direction label retired from the report.** Rendering
   only (`markdown_report.py`): the §2 Scorecard drops the per-block Direction
   column and shows the metrics carrying each block's concern instead, ranked by
   weight × concern. `DIRECTION_POSITIVE_BELOW` / `DIRECTION_NEGATIVE_ABOVE` are
   **unchanged**, `_direction()` is unchanged, and `BlockScore.direction` remains
   on the API. **No 0-100 score moved** — the regenerated golden report shows
   every block score identical and only the column swapped, and
   `calibration_snapshot.json` needed no regeneration (it records the composite
   direction and the band values, neither of which changed).

   Logged here rather than deferred to window close because it is a correctness
   retraction, not tuning: the bands are percentiles of the composite
   distribution, the block anchor tables share no common scale with it, and no
   evaluation output reads the label (`backtest_results.csv` has no direction
   column). It therefore cannot fit the eval. The two non-label consumers of the
   constants — the Track-3 sweep flag gate (`wide_sweep.py`) and the Capital
   Integrity takedown caveat (`report_builder.py`) — are deliberately untouched,
   so Track 3's flag set is unaffected. Re-anchoring the composite bands remains
   deferred to window close as `calibration_report.md` already states.

2. **2026-09-22 — distress-scored components reach the flag list.** Flag
   generation only (`app/core/pipeline.py::_generate_flags`). P0-9 has the scorer
   keep a metric that is NOT_MEANINGFUL *because its denominator signals
   distress* (net loss with cash burn; non-positive EBITDA with net debt) at
   its maximum concern with full weight — but the flag generator dropped every
   component whose metric had no value, which is exactly those. "Non-positive
   EBITDA with net debt" scored 90 and could never appear on the card. A
   `distress_signal` component is now flagged like any other red-threshold
   component, with a title naming the reason and a detail saying the ratio is
   undefined. Zero-weight exclusion (P0-13) and benign not-meaningful guards
   are unchanged.

   **No anchor, weight, block score or composite moved**: the scorer already
   counted these components; only their presence in `red_flags` changed. The
   calibration snapshot pins scores, not flags, and needed no regeneration;
   the golden StretchCo report has no distress-scored component and is
   byte-identical. Proof test: `tests/unit/test_distress_flags.py`
   (invariant: weight>0 ∧ concern≥red ⇒ exactly one red flag, value or not).

   Evaluation outputs that DO read flags: `wide_sweep.py` writes `n_red_flags`
   / `red_flag_metrics` into `data/sweep/sweep_results.csv` and the
   adjudication worksheet, and the distressed-control / survivorship pilots
   record `n_red_flags`. Track 3's flag *gate* is the composite direction band
   (`FLAG_THRESHOLD`), so the 29-name flag set is unaffected; but those
   count columns are not comparable before/after for any name with a
   distress-scored component, and a rerun of the sweep or pilots must say so.

3. **2026-09-22 — field registry; `pit.py` touched at import level only.**
   The field ontology (which XBRL tags back each field, the SG&A/D&A
   composites, the total-debt roles, units, additivity, split adjustment)
   moved from literal tables in `companyfacts_mapper.py` into one data module,
   `app/services/ingestion/fields.py`. Every old table is now a view of it
   under its old name, in its old order. `pit.mapped_tags()` — the tag set
   every backtest trims to — delegates to `fields.all_tags()` instead of
   re-walking the tables by hand; it is the only change in a flag-only file.

   **No mapped value, score or flag moved.** Proof test:
   `tests/unit/test_field_registry.py` holds verbatim copies of the replaced
   tables and asserts every view equal to them *including order* (candidate
   order breaks coverage ties), and asserts `mapped_tags()` equal to the old
   hand-built set concept for concept. Before/after, `build_dataset` output
   (dataset and diagnostics) on the three real fixtures, on PIT cuts of them,
   and on synthetic debt/composite payloads was byte-identical;
   `calibration_snapshot.json` and the golden report were not regenerated.

   `scripts/make_real_fixtures.py` now trims to the same registry set, which
   adds the finance-lease tags it had never kept. The committed fixtures are
   **not** regenerated in this window: doing so would move `total_debt` for
   filers that report finance leases, and the calibration snapshot is
   computed from those files. Regeneration is a window-close item.

4. **2026-09-22 — a dead mismatch trigger removed (`narrative/mismatch.py`).**
   The profitability-vs-cash-conversion spec listed `fcf_to_net_income` among
   its trigger metrics. Concern-triggered specs read `concern_by_name`, which
   `pipeline._financial_concerns` fills only for metrics placed in a scoring
   block, and `fcf_to_net_income` is not one — so it could never trigger,
   whatever its value. It is removed from the spec; the spec still fires on
   `cfo_to_net_income` and `fcf_margin_trend` exactly as before. Two
   unreachable flag phrases (`sbc_to_revenue`, `adjustment_recurrence_ratio`:
   flags come from scored components only) were deleted from
   `app/core/pipeline.py` in the same change, which is not a flag-only file.

   **No mismatch, flag, score or rendered line changed.** Proof tests:
   `tests/unit/test_metrics_registry.py::test_mismatch_triggers_can_fire`
   (every concern trigger is a scored metric) and
   `::test_the_concern_map_carries_scored_metrics_only` (an extreme
   `fcf_to_net_income` gets no concern entry); the golden report and
   `calibration_snapshot.json` are byte-identical. Making `fcf_to_net_income`
   actually trigger — by scoring it or triggering on its raw value — would be
   a signal addition and is a window-close item.

5. **2026-09-22 — narrative evidence rows name their filing
   (`narrative/evidence.py`, `narrative_metrics.py`, `mismatch.py`).** Every
   narrative ledger row claimed `source="period documents"` although each
   document already carried its form and accession. The ledger now locates
   each quoted window in the documents of its own period and names them
   ("10-Q 0000…; 8-K 0000… ex99_1.htm"); computed rows (KPI added/removed,
   disclosure-volume reduction) name the period documents they were derived
   from; a window it cannot place, or documents with no recorded source, say
   so explicitly. `DocumentRecord` gains optional `accession`/`form`/`filed`.

   **No detector, term list, metric, finding, flag or score changed** — only
   the `source` string of ledger rows, which the markdown report does not
   render (it reaches the API's evidence JSON). Proof tests:
   `tests/unit/test_evidence_attribution.py` (every quoted row names only
   documents of its period containing its windows, over generated filings);
   the golden report and `calibration_snapshot.json` are byte-identical, and
   the AAPL/KO/CRM reports are byte-identical to the previous main.

Mid-window changes (0.3.0 window, closed): the window ended with the P0
correction program (PR #2) rather than by reaching its planned sample size —
see closure note above.

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
