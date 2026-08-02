# Project State Assessment — 2026-08-01

Status: complete · Basis: three independent deep audits of the full repository (architecture/pipelines, signal/scoring inventory, validation/journal/surfaces) plus the six 2026Q3 research surveys and the 2026Q2 live-season record (11 runs).

This document treats the existing project as a hypothesis, not as something to defend. Companion documents: [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), [VALIDATION_STRATEGY.md](VALIDATION_STRATEGY.md), [ROADMAP_2026Q3.md](ROADMAP_2026Q3.md).

---

## 1. What we currently have

A ~12,700-line Python system with:

- **Ingestion**: SEC companyfacts client + a genuinely good pure XBRL mapper (tag-candidate selection, YTD differencing, fy-minus-3q derivation, 52/53-week handling, per-field diagnostics). Reconciles to the dollar against AAPL/MSFT/KO 10-Ks; 11 real-data bug classes each carry a regression test. Document fetcher (MD&A, Risk Factors, 8-K EX-99). New offerings reader (S-1/S-3/424B timeline) with the best provenance in the codebase.
- **Signals**: ~43 financial metrics (accruals, Beneish, working capital, balance sheet, capital structure, capex) + 11 textual detectors, all under a uniform metric contract (never raises; explicit MISSING_DATA / NOT_MEANINGFUL).
- **Scoring**: 8 concern blocks → composite 0–100, hand-set anchors, weights partially calibrated from a 75-company walk-forward backtest.
- **Reporting**: deterministic 11-section markdown report; golden-file tested; legally-constrained language enforced by tests.
- **Journal**: thesis-before-report lock (CLI + web, shared store, regression-tested), 4-stage entry schema (BEFORE/lock/AFTER/OUTCOME), 20-case gate.
- **Validation record**: eight completed experiments with pre-committed kill criteria that were honored — survivorship pilot, distressed-survivor control, restatement control, narrative controls, timing analysis, KPI Phase-4 + isolation spike. Plus an 11-run live season with an honest performance review.
- **Research base**: six literature surveys (2026Q3) covering non-GAAP adjustments, textual measures, deterministic filing components, XBRL normalization, insider/offerings, and calibration/journaling — each finding tagged with evidence strength.
- 282 offline tests passing in 0.43s. No CI.

**Settled verdict from the project's own experiments** (docs/what_this_engine_can_and_cannot_do.md): the engine works as a **distress thermometer** (75–83% of eventual failures flagged ≥p90 pre-event vs 13.5% base) and a **contemporaneous high-severity-disclosure monitor** (30% restaters vs 0% clean). It does **not** work as a failure predictor (distressed survivors flag identically), a single-firm misstatement detector (1/5 pure-forensic catches; MiMedx missed), or a ranking (top-quintile tail screen only, 44.5% FP).

---

## 2. What is strong

1. **Negative-result hygiene.** Every inflated finding was deflated by a purpose-built control with pre-committed criteria, and the kill decisions were honored (KPI-drift shelved despite being the only "predictive" result). This is the rarest asset in the repo and the reason anything else here can be trusted.
2. **The XBRL mapper and metric contract.** Deterministic, offline-testable, reconciled against real filings, degrades explicitly. The audit that hunted for YTD-differencing bugs found the mapper already handles the trap correctly.
3. **The journal lock.** Thesis-before-report is mechanically enforced in both surfaces, regression-tested including its historical false-trip bug.
4. **Legal framing as code.** Banned-vocabulary tests and a grounding validator (evidence-id citation, number-grounding, rejection-not-repair) — the "no accusations" constraint is enforced, not aspirational.
5. **The live-season audit loop.** The 2026Q2 record shows the system's real value: same-day 10-Q evidence retrieval, dilution/offering discoveries (MXL +11%/q dilution, KTOS's $84 raise, META's capex regime shift pre-print), and the correction workflow around engine artifacts. The season review's own conclusion stands: *the audit loop is the product; the score is the weakest layer.*
6. **The offerings reader** — built in direct response to the season's worst miss (FPS), evidence-only, full provenance (accession + filing date), tested on real prospectus fixtures.

---

## 3. What is weak

### 3.1 Correctness defects in live outputs (P0 — can make current reports wrong)

Confirmed by the architecture audit, with file:line references:

| # | Defect | Location | Effect |
|---|---|---|---|
| P0-1 | `net_debt_to_ebitda` divides stock by ONE QUARTER of EBITDA against annual-scale anchors | `balance_sheet.py:19,30-31`; anchors `scoring_config.py:128` | ~4x overstated; was GLW's top red flag at concern 90 (7.39 vs ~1.85 true). Known since July; still unfixed |
| P0-2 | Beneish M-score: quarterly components vs annual −2.22/−1.78 cutoffs, all-or-nothing composition | `beneish.py:230-264` | Highest-weighted Earnings Quality component on wrong scale; one missing input silently kills it and renormalizes the block |
| P0-3 | `total_accruals`/`beneish_tata` quarterly values vs annual-scale anchors | `scoring_config.py:68,71` | Accrual concern systematically **under**stated ~4x — mirror image of P0-1 |
| P0-4 | All pair metrics compare sequential quarters; no YoY mode exists | `registry.py:81-83` | Seasonality fabricates DSRI/SGI/spread signals at every Q4/Q1 boundary (the GLW FCF-trough misread class) |
| P0-5 | Latest-filed-wins rewrites history in live reports | `companyfacts_mapper.py:245-253` | Restatements erase their own footprints; trend metrics measure restated, not as-reported, deterioration |
| P0-6 | 8-K earnings releases mislabeled to the prior quarter between 8-K and 10-Q | `edgar_documents.py:215-231` | Mismatch detectors compare narrative to the wrong period's metrics, silently |
| P0-7 | High-growth caveat fires only at +40% **QoQ** (intended: annual) | `scoring_config.py:174` | The main false-positive control is effectively disabled |
| P0-8 | Discredited KPI-definition extractor still live in reports | `narrative_metrics.py:134` | Phase-4/spike concluded its hits are mostly extraction artifacts; it still ships |
| P0-9 | NOT_MEANINGFUL guards + renormalization amputate the score's top exactly in distress | `engine.py:114,133` + guards | NI≤0 deletes CFO/NI; EBITDA≤0 deletes leverage; weight flows to less-alarming survivors |
| P0-10 | Total-debt understatement when current-portion split absent; leases never included | `companyfacts_mapper.py:445-478` | Compounds or offsets P0-1 unpredictably per company; disclosed only in never-rendered diagnostics |
| P0-11 | EX-99 exhibit selection takes `candidates[0]` | `edgar_documents.py:179` | Can ingest the tables exhibit instead of the release; distorts all narrative densities |
| P0-12 | 24h caches with no as-of stamp; offerings cutoff uses `date.today()` | `sec_client.py:61-67`, `offerings.py:197` | Filing-day reports can analyze pre-filing data while dated today; offerings appendix non-PIT |
| P0-13 | Zero-weighted metrics still generate red flags | pipeline flag path | `working_capital_swing_to_income` (excluded as wrong-signed) appeared in MXL's flags at 85/100 |

### 3.2 The composite score cannot discriminate — by construction

Measured: 11 live runs landed 31–58, all "Mixed"; the cleanest company fell 6%, the worst-scored rallied 10%. Root causes (signal audit):

1. Two-stage weighted averaging over ~35 components that reduce to **~15–18 independent economic quantities** — correlated errors stack, independent variation diversifies away. (NI−CFO)/assets is scored three times inside Earnings Quality; receivables-vs-revenue is 80% of Revenue Quality via three disguises.
2. Anchor geometry: neutral values map to 20–35 concern → floor ~20–30 for a clean company; clamping discards extremity beyond the last anchor → ceiling.
3. P0-9: the guards remove the most damning metrics exactly in distress.
4. A universal noise floor: `adjustment_recurrence` fired 79–88 on 11/11 companies (+~2.8 composite points for everyone, zero discrimination).
5. No cross-sectional or reference-class normalization anywhere (`SECTOR_ANCHOR_OVERRIDES` intentionally empty).
6. The v0.3 "fix" refit the direction bands to the compressed distribution — relabeling, not decompressing.

### 3.3 The report surface inverts the project's own evidence

What a reader sees in 30 seconds: the composite score (measured near-zero decision value) and a red-flag list whose top entries have included the P0-1 artifact and `adjustment_recurrence` (proven noise: 90% restaters vs 91% clean). The two **validated** signals — distress components and high-severity-disclosure emergence — are buried in §6. The §5 deltas and findings that carried the season's value sit mid-report. A 90-second-card redesign was specified 2026-07-03 and never built.

### 3.4 Provenance is collected, then discarded

- `RawFact` carries filed/form per fact; `FieldDiagnostic` carries tag/method — all die at `PeriodFinancials`, which has no per-field source. `EvidenceEntry` has formula+inputs but no accession, filed date, or XBRL concept.
- `DocumentRecord.source` contains `"10-Q {accession}"`, but every narrative ledger `add()` hard-codes `source="period documents"`.
- `IngestionDiagnostics` never joins `AnalysisResult`: coverage causes are stdout-only; the web-journal surface shows nothing.
- Only the offerings reader carries provenance end-to-end — and it is not in the pipeline (script-append only), so Capital Integrity scored FPS 10/100 with the sponsor's four sell-downs sitting in an unconnected appendix.

### 3.5 The journal cannot yet prove the central claim

The only instrument that can answer "does the engine improve decisions" has: n=1 locked case, empty AFTER block, 0 outcomes against a 20-case gate. The schema is free-text: no machine-checkable assumptions (concept + comparator + threshold + window + resolution date), no falsifiers, no probabilities (so no Brier scoring), no reference class, no post-lock tamper evidence. The live journal and the (never-executed) blind-validation framework use incompatible schemas. The one real entry carries a documented contamination caveat the schema has no field for.

### 3.6 Two divergent report paths

`scripts/generate_report.py` (offerings appendix + coverage printout) vs `journal/reporting.build_report` (neither). Web-journal users get a strictly poorer artifact than CLI users.

---

## 4. What is unnecessary

- **`adjustment_recurrence_ratio` + `recurring_adjustment_terms`** — 100% live FP; 35% of the Narrative block. Remove from scoring outright (replacement design exists: per-issuer adjustment ledger + n-gram novelty gate).
- **`beneish_tata` and `beneish_dsri` as standalone scored components** — pure double-counting; retain inside M-score only.
- **`working_capital_swing_to_income`, `buyback_offset_ratio`** — wrong-signed, already zero-weighted; delete from config and flag path (P0-13).
- **`sbc_to_revenue`/`sbc_to_cfo` as score inputs** — measured wrong-signed; 45% of a scored block. Evidence-only (where MXL's 5.7x SBC/CFO was a genuine season win).
- **`guidance_shift`, `defensive_tone_change` as score inputs** — negation-blind keyword stance counting / unvalidated hand lexicon in an "ENTIRELY UNCALIBRATED" block.
- **`asset_quality_proxy`, `intangibles_to_assets` as score inputs** — sector-membership detectors without sector norms.
- **The dead JSON API** (`/analyze`, `/report` on caller-supplied datasets) — used only by its own tests.
- **KPI-drift live wiring** — the shelved detector still runs in reports (P0-8); the hardened extractor/adjudicator built for Phase 4 sits unwired. Either wire the hardened evidence-only version or unwire both.
- **Script sprawl** — the shelved-KPI one-offs and the self-labeled throwaway spike belong under an `archive/` prefix with their docs.
- The **thesis-monitor preview template** (mockup of an unaccepted proposal).

Removals are subtractions from scoring/surfaces, not deletions of evidence: several (SBC, buybacks, tone) remain valuable in the evidence plane.

## 5. Biggest unresolved risks

1. **The central claim is unproven and currently unprovable at the current journal cadence.** One season produced one locked case. At this rate the 20-case gate is >1 year away; the redefined 8–10-case gate is still multiple quarters. Risk: the project runs on anecdotes (META, KTOS) that its own protocol says to distrust.
2. **Known-wrong outputs ship daily.** P0-1 has been documented in two places since July and still generates top red flags. Every report shipped with it erodes the only thing that distinguishes this project: trustworthiness of the evidence.
3. **Silent degradation.** Document-fetch failures degrade to "fewer documents" indistinguishable from "filer didn't disclose"; offerings appendix vanishes on any exception with one console line; caches can serve pre-filing data on filing day.
4. **Single-operator process risk.** No CI: the 282 tests and golden gates bite only when run by hand. The adjudication track has been stalled at 0/29 since 2026-07-03.
5. **Restatement blindness in live mode** (P0-5): the tool most interested in deterioration silently accepts restated history — the one data-shape a deteriorating company controls.

---

## 6. Proven vs asserted (the ledger that governs the roadmap)

**Proven (engineering standard):** XBRL ingestion correctness on reconciled names; the journal lock; legal-framing enforcement; deterministic reproducible scoring under the frozen config.

**Established directionally (small n, honest caveats, consistent with the published literature):** distress detection works; death prediction doesn't; single-firm misstatement detection doesn't; high-severity-disclosure emergence discriminates (30% vs 0%) but is contemporaneous, not early; adjustment-keyword detection is noise; KPI-drift is ~1/10 genuine and shelved.

**Asserted, not proven:** that the engine changes decisions (n=1, no outcomes); that the distress thermometer routes attention usefully; that the season's qualitative wins generalize; everything in the blind-validation framework (0 events run).

The gap between the first two categories and the third **is** the project's remaining task. The next phase must be judged on moving items from "asserted" to "established," not on adding signals.
