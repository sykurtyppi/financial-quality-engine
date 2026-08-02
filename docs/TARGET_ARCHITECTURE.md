# Target Architecture — 2026-08-01

Status: proposed (approved for planning; implementation gated by [ROADMAP_2026Q3.md](ROADMAP_2026Q3.md)) · Basis: [PROJECT_STATE_ASSESSMENT.md](PROJECT_STATE_ASSESSMENT.md) + six 2026Q3 surveys + gap-driven research pass.

Evolutionary, not a rewrite: every layer below maps onto existing code, and the migration path preserves the 282-test suite. Anything marked **NEW** has no current implementation.

---

## 1. What the product is

**Problem.** An investor holding or considering a position around an earnings event (or other material filing) cannot read everything the company filed, cannot trust summaries that hallucinate, and is systematically vulnerable to (a) missing deterioration evidence that was available, and (b) overreacting to alarming-looking artifacts that are benign. Both errors are expensive; the second is more frequent.

**Who it is for.** A single analyst/investor (today: the operator), before or on the day of an event. Not a screener over the market; not a fund workflow tool.

**What it must tell that person in 30 seconds** (the decision card, in this order):

1. **What is NEW or WORSENED since last period** — each item a provenance-linked fact (accession, filed date), with its change state and any benign-explanation candidates attached.
2. **Distress state** — the validated thermometer, expressed against a reference class ("p87 of its own 5-year history; top decile of same-year filers"), never as a probability of failure.
3. **Capital-markets and insider facts in the window** — offerings, secondary sell-downs, cluster purchases, plan terminations, filing-behavior events (NT, 4.02, auditor change).
4. **What was checked and found CLEAN** — assumptions examined and *not* violated, stated as prominently as violations. Preventing a false alarm is a first-class output (the Barber–Odean framing: value = bad decisions not taken).

**What it must NOT attempt:** price prediction, buy/sell/hold advice, a single quality grade presented as meaningful, misstatement accusations, probabilities it cannot calibrate.

**Success =** journal-measured decision impact (the only metric that counts), plus: corrections rate ≈ 0 on shipped facts, false-positive rate on flags low enough that the operator stops hand-discounting them, and time-to-evidence on filing day measured in minutes.

**Framing.** The engine does *detection* (what does the filing record show), *triage* (what deserves attention first), *investigation support* (evidence + provenance + analyst questions), and *calibration* (journal). It does not do prediction. The prior framing — "distress thermometer + contemporaneous disclosure monitor" — survives the audit and is retained, with one extension the season proved: **the same-day evidence audit loop around an event is the product's core motion**, and the architecture below is organized to serve it.

---

## 2. Layer diagram

```
 L0  SOURCE               sec_client (shared limiter, retry, cache w/ as-of stamps)
 L1  ACQUISITION / PIT    raw facts + documents + filing index, filed-dates preserved,
                          append-only vintage store, unified as_of for live & replay
 L2  ACCOUNTING SEMANTICS companyfacts mapper (+ per-field provenance), TTM constructor,
                          fiscal calendar, comparator service (QoQ | YoY lag-4 | TTM),
                          debt composition, funding context (grants/ITC/supplier finance)
 L3  DETERMINISTIC        ~20 deduplicated financial signals under the metric contract,
     SIGNAL ENGINE        each bound to a taxonomy spec (§5)
 L4  CHANGE ENGINE        state machine per signal: new | worsened | improving | cleared |
                          unresolved | data_artifact; seasonal baselines; vintage diffs
 L5  REFERENCE CLASS      own-history percentiles; same-year cross-sectional distributions
                          (XBRL frames, precomputed); outcome base rates
 L6  EVENTS & CAPITAL     8-K item rules (4.02/4.01/2.06/NT), filing lag, offerings
     MARKETS / INSIDERS   timeline, Form 4 (clusters, derived lateness, plan terminations)  **NEW in pipeline**
 L7  NARRATIVE /          evidence-only validated measures: high-severity emergence,
     DISCLOSURE           adjustment ledger + n-gram novelty, risk-factor set-diff,
                          numbers-to-words density, decomposed disclosure volume
 L8  CONSISTENCY          cross-plane contradiction checks (narrative↔metrics,
                          buyback↔share count, capital-integrity↔offerings, guidance↔actuals)
 L9  EVIDENCE LEDGER      one unified, provenance-complete, as-of-stamped ledger (§6)
 L10 TRIAGE               tiered flag counts + thermometer percentile; NO composite grade (§7)
 L11 DECISION SURFACE     90-second card → full report → appendices; checked-and-clean section
 L12 JOURNAL &            preregistration schema (machine-checkable), outcome resolution,
     CALIBRATION          Brier/calibration when n permits
 L13 VALIDATION HARNESS   fixtures, PIT replays, public benchmarks, placebo/ablation runners
```

Layers L0–L3 exist today (with the P0 fixes applied). L4 exists as fragments ("what changed" + trend metrics) and becomes explicit. L5, L6-insiders, and the L9 unification are new. L10–L11 are a re-ordering of what exists. L12–L13 exist as partial schemas/scripts and get completed.

**Where LLMs are allowed:** nowhere in L0–L9 fact production. An LLM may (a) summarize/synthesize ledger items *with grounding validation* (the existing `grounding.py` rejection-not-repair contract), and (b) adjudicate candidate textual findings as the KPI Phase-4 design did — always downstream of deterministic extraction, always evidence-cited, never creating financial facts. This is already the repo's stated principle; the architecture keeps it.

---

## 3. Component specifications

Format per component: **R** responsibility · **I/O** inputs→outputs · **Contract** · **Persist** · **PIT** · **Provenance** · **Failure** · **Test** · **Deps**.

### L0 SOURCE — `app/services/ingestion/sec_client.py` (harden)
- **R**: All EDGAR HTTP. One client instance per process, injected everywhere (kill the private `_get` coupling and per-instance limiters).
- **I/O**: URL → bytes/JSON + `FetchMeta{url, fetched_at, cache_hit, etag}`.
- **Contract**: every response wrapped with `FetchMeta`; callers never see raw requests.
- **Persist**: cache keyed by URL + schema-version; JSON TTL 24h **except** submissions/index on an event day (explicit `fresh=True`); Archives immutable-forever.
- **PIT**: `fetched_at` recorded on everything; reports print a data-as-of line (fixes P0-12).
- **Failure**: retry ×3 w/ backoff on 5xx/429; then raise `SecClientError` — loud, never silent.
- **Test**: existing + retry/backoff unit tests with a fake transport.
- **Deps**: none.

### L1 ACQUISITION / PIT — `app/services/ingestion/` (extend)
- **R**: Produce raw, vintage-preserving inputs: companyfacts raw facts (with `filed`), submissions index, documents, offerings filings, Form 4 index (**NEW**).
- **I/O**: ticker/CIK + `as_of: date` → `RawFactSet`, `FilingIndex`, `DocumentRecord[]` (now carrying `accession`, `filed_date` as first-class fields), `OfferingFiling[]`, `Form4Record[]`.
- **Contract**: `as_of` is required on every acquisition call. Live = today; replay = historical. One code path (today's split — PIT-only-in-backtest — is the root of P0-5/P0-12).
- **Persist**: append-only vintage store for companyfacts snapshots (dated JSON per CIK per fetch; enables restatement diffing, survey-5's little-r leading indicator). Documents cached immutable.
- **PIT**: facts filtered `filed <= as_of` in replay; in live mode, vintage stored so future replays are honest.
- **Provenance**: accession + form + filed on every record. (DocumentRecord gains `accession`, `filed: date`, `period_end_source: "xbrl" | "filing_index"`.)
- **Failure**: fetch failure of a document class → explicit `AcquisitionGap{kind, reason}` in diagnostics, distinguishable from "filer didn't disclose" (fixes silent degradation).
- **Test**: fixture-driven; PIT filter tests exist (`backtesting/pit.py` promotes into here).
- **Deps**: L0.

### L2 ACCOUNTING SEMANTICS — `companyfacts_mapper.py` + new `normalization/`
- **R**: `RawFactSet → PeriodFinancials[]` with per-field provenance; TTM series; comparator selection; fiscal calendar; debt composition; funding context.
- **I/O**: raw facts → `PeriodFinancials` where every field is a `SourcedValue{value, tag, accession, filed, method: direct|ytd_diff|fy_minus_3q, unit}` (fixes the provenance discard); plus `TtmFinancials` per quarter (sum of 4 rolling quarters, gap-refusing); plus `FiscalContext{fye_month, week_convention, is_53w, q_label}`.
- **Contract additions**:
  - **Comparator service**: every change-form signal declares its comparator: `qoq` | `yoy_lag4` | `ttm_vs_ttm` | `latest_vs_seasonal_mean` (same-fiscal-quarter mean). Default for seasonal-sensitive metrics = `yoy_lag4`. Q4 never compares to interim quarters (Binz–Kapons). Fixes P0-4.
  - **TTM constructor**: any stock-over-flow ratio must use TTM flow (fixes P0-1/P0-2/P0-3 as a class, not one-off). 668/2000 covenants use 4Q EBITDA; zero use single-quarter.
  - **Debt composition**: explicit `DebtBreakdown{lt, current_portion, short_term, leases_operating, leases_finance, gaps[]}` — gaps rendered, not stdout-printed (fixes P0-10).
  - **Funding context** (**NEW**, evidence-plane): `GrantsReceivable{Current,Noncurrent}` deltas vs net income; supplier-finance ASC 405-50 rules A–D; ITC/prepayment lexicon hits in CF footnotes — attached to cash-conversion evidence as benign/aggravating context (the AMKR class).
- **Persist**: none beyond L1 cache (pure).
- **PIT**: mapper is pure over the as-of-filtered fact set.
- **Failure**: unchanged (explicit diagnostics), but `IngestionDiagnostics` joins `AnalysisResult` (fixes stdout-only coverage).
- **Test**: existing 274-line mapper suite + new TTM/comparator/debt fixtures; Apple FY2024 YTD regression fixture.
- **Deps**: L1.

### L3 DETERMINISTIC SIGNAL ENGINE — `app/services/formulas/` (prune + fix)
- **R**: Compute the deduplicated signal set (§5) under the metric contract.
- **Changes from current**: remove standalone `beneish_tata`/`beneish_dsri` scoring; merge dilution pair; thin the cash-conversion quadruplet to `cfo_to_net_income (TTM)` + `fcf_margin (TTM + seasonal comparator)`; M-score computed on TTM/annual pairs only; keep guards but emit `RefusedMetric{name, reason, direction_hint}` instead of silent renormalization (see §7).
- **Contract**: `MetricResult` gains `comparator`, `basis: quarterly|ttm|annual`, and `spec_id` (taxonomy binding). Schema enforces `status==OK ⇒ value is not None`.
- **Test**: existing formula suites, updated fixtures; a **basis-mismatch linter test**: no stock/flow ratio may declare `basis: quarterly`.
- **Deps**: L2.

### L4 CHANGE ENGINE — **NEW module**, absorbs "what changed" + trend logic
- **R**: For every signal and every narrative/event finding, assign a state: `new | worsened | improving | cleared | unresolved | data_artifact`, with the comparator that justifies it.
- **I/O**: current + prior `MetricResult`s/findings + vintage store → `ChangeRecord{spec_id, state, delta, comparator, prior_ref, note}`.
- **Vintage diff** (**NEW**): compare latest-filed values for prior periods against the values as originally filed (from the vintage store): any silent prior-period change ≥ threshold → `data_artifact/restated` evidence item (turns P0-5 from a blindness into a detector — survey 5: little-r revisions are a leading indicator, Choudhary et al.).
- **Failure**: insufficient history → `unresolved`, never a fabricated state.
- **Test**: state-machine table tests; restatement fixture (Kraft Heinz Big-R case from survey 5).
- **Deps**: L2, L3, L1-vintage.

### L5 REFERENCE CLASS — **NEW module** `app/services/reference/`
- **R**: Convert raw values into calibrated context: own-history percentile (≥8 quarters), same-year cross-sectional percentile (from SEC XBRL **frames** API, precomputed quarterly distributions for the ~15 core ratios), and outcome base rates for report language ("firms at this decile restated within 2y at X% vs base Y%" — only where a survey-verified base rate exists; never invented).
- **Persist**: `data/reference/` parquet/JSON distributions, versioned, rebuilt quarterly.
- **PIT**: distributions dated; replay uses the vintage ≤ as_of.
- **Failure**: no reference class available → report says "no reference class", never a bare scary number (this is the Brown–Tucker same-year benchmarking fix generalized).
- **Test**: distribution snapshot tests; leakage test (a frame built in Q3 must not be usable at a Q2 as_of).
- **Deps**: L0 (frames), L3.

### L6 EVENTS & CAPITAL MARKETS / INSIDERS — promote offerings; **NEW** 8-K rules + Form 4
- **R**: Deterministic event stream from filing metadata + minimal text rules.
- **Components**:
  - **Filing behavior**: NT 10-Q/10-K with extension-date arithmetic (missed extended deadline flag); 8-K Item 4.02 (and NT→4.02-within-30d chain); Item 4.01 with resignation-vs-dismissal split; Item 2.06; filing-lag drift vs own history. (Rules from survey 4; the 4.02 detector exists in `backtesting/events.py` — promote to live.)
  - **Offerings**: existing reader, moved into the pipeline (input to L8/L9, not a script append), `as_of` instead of `date.today()`, full-history index (not recent-block-only), S-3 shelf filings surfaced as early-warning distinct from 424B takedowns.
  - **Insiders** (**NEW**): Form 4 ingestion; cluster purchases (≥2 distinct non-A/M/F insiders, same/consecutive days); derived lateness (TRANS_DATE→FILING_DATE > 2 business days; never the 99.5%-blank timeliness field); 10b5-1 plan-termination language; Form 144 planAdoptionDate. All evidence-plane. Presentation rule: "sold under a plan" is never framed as suspicious.
- **Failure**: index unavailable → explicit `AcquisitionGap`; partial parses recorded in diagnostics (offerings reader already does this).
- **Test**: existing offerings fixtures + new Form 4/8-K item fixtures with real filings.
- **Deps**: L0, L1.

### L7 NARRATIVE / DISCLOSURE — `app/services/narrative/` (prune to validated set)
- **R**: Evidence-only textual findings. Every measure must satisfy both survey principles: **change > level** and **density > presence**, benchmarked same-year where cross-sectional.
- **Keep**: `high_severity_disclosure` emergence (the best-designed detector; validated 30% vs 0%); mismatch templates (L8); `kpi_removals` (change-based, season-validated).
- **Replace**: adjustment keywords → per-issuer **non-GAAP adjustment ledger** (Doyle–Lundholm–Soliman lineage) + 8-gram novelty gate for recurring language; `risk_factor_expansion` word-ratio → **ADD/REMOVE set-diff** + Item-1A specificity (entity-density regex classes); `disclosure_volume_change` retained but decomposed (which section moved) before it may imply anything.
- **Add** (cheap, validated): per-section YoY **cosine similarity** (MD&A, Risk Factors) — pure Lazy Prices, the only outcome-validated change detector in the literature (no embedding approach has beaten it in a replicated study; gap research Gap 3); MD&A numbers-to-words density (Siano–Wysocki recipe) as within-filer change.
- **Remove from any scored path**: tone/guidance keyword detectors (evidence-only at most, clearly labeled unvalidated).
- **Failure**: extraction failure → `AcquisitionGap`, not silent MISSING (5/16 clean-control companies failed extraction silently — that class ends).
- **Test**: detector fixtures + the public 3,737-filing segmentation benchmark (κ=0.92) as the extraction-quality gate (L13).
- **Deps**: L1 documents, L5 same-year benchmarks.

### L8 CONSISTENCY — `mismatch.py` (extend)
- **R**: Cross-plane contradiction checks, each rendered as a review prompt with both sides cited: narrative-claim↔metric (existing 4 templates), buyback-claim↔share-count (existing), **capital-integrity↔offerings-timeline** (NEW — the FPS fix: any equity takedown or ≥50%-secondary event in-window forces a caveat on dilution/SBC evidence), **guidance↔actuals** (NEW, deterministic: compare prior quantified guidance from 8-K EX-99 to realized XBRL actuals; per-firm guidance-error history — accuracy persistence is peer-reviewed and replicated, gap research Gap 4), **ER↔10-Q numeric diff** (NEW: shared line items in the earnings release vs the subsequent 10-Q — any silent revision is a flag; Calcbench-documented phenomenon). Explicitly rejected: Larcker–Zakolyukina deception cues (6–16% over random, no replication).
- **Deps**: L3–L7.

### L9 EVIDENCE LEDGER — unify (`schemas/report.py` rework)
- **R**: THE single output substrate. Everything the user sees is a ledger item.
- **Contract**: `EvidenceItem{id, plane: accounting|funding|capital_markets|insider|filing_behavior|narrative|consistency, claim, value, method{formula|rule|extractor, version}, inputs, provenance{accession, form, filed, tag?, url, excerpt?}, as_of, change_state, reference{own_history_pct?, cross_sectional_pct?, base_rate_ref?}, confidence: high|medium|low (enum, enforced), benign_candidates[], validation_status: validated|directional|unvalidated}`.
- **Persist**: ledger serialized with every report (JSON alongside markdown) — reports become auditable artifacts, and the journal can reference item ids.
- **Failure**: an item missing provenance fails schema validation — provenance is load-bearing, not decorative.
- **Test**: schema round-trip; a **provenance-completeness gate** in CI (no item without accession+filed unless plane=derived).
- **Deps**: everything above.

### L10 TRIAGE — replace the composite (§7 below).

### L11 DECISION SURFACE — `markdown_report.py` rework + journal web
- **R**: The 90-second card first: (1) NEW/WORSENED items (max ~7, severity-tiered), (2) thermometer + reference class, (3) events/capital-markets window, (4) CHECKED-AND-CLEAN list, (5) data-quality line (coverage %, gaps, as-of stamps). Full 11-section report follows as appendix. One report builder serving CLI and web (kills the divergent paths).
- **Test**: golden card fixtures; the existing banned-language and grounding gates apply to every surface.

### L12 JOURNAL & CALIBRATION — schema upgrade (detail in [VALIDATION_STRATEGY.md](VALIDATION_STRATEGY.md) §5)
- Machine-checkable assumption rows, falsifiers, probability, reference class, hash-locked BEFORE block, contamination field, outcome resolution against assumption rows. Backward-compatible: old free-text fields remain; new fields optional-but-nagged.

### L13 VALIDATION HARNESS — `scripts/validation/` (consolidate)
- Promote the reusable experiment runners; add benchmark suite (segmentation gold set), placebo-signal runner, ablation runner, PIT-replay runner. Archive one-offs under `scripts/archive/`.

---

## 4. Persistence map

| Store | Content | Format | PIT rule |
|---|---|---|---|
| `data/cache/` | EDGAR responses + FetchMeta | JSON/HTML | TTL per class; immutable archives |
| `data/vintage/` | dated companyfacts snapshots per CIK | JSON, append-only | never overwritten |
| `data/reference/` | quarterly cross-sectional distributions | parquet/JSON, versioned | dated; replay uses vintage ≤ as_of |
| `reports/` | markdown + evidence-ledger JSON | per ticker-date | as-of stamped |
| `journal/entries/` | entries + BEFORE-block hash | markdown + sidecar hash | lock enforced |
| `data/validation/` | benchmark results, calibration snapshots | CSV/JSON | versioned with config |

---

## 5. Signal taxonomy (canonical)

Every signal binds to a spec with: economic hypothesis · calculation · data · baseline/comparator · reference class · direction · confidence · known FP conditions · validation status. The full spec table is the config of L3/L6/L7; summary:

### Accounting (scored plane — the thermometer inputs)
| Spec | Hypothesis | Comparator/basis | Validation status |
|---|---|---|---|
| total_accruals (TTM) | accruals inflate earnings ahead of margin deterioration | TTM, own-history + same-year pct | backtest IC −0.20/−0.25 (directional) |
| cfo_to_net_income (TTM) | cash confirms income | TTM | directional; funding-context caveats |
| fcf_margin (TTM) + seasonal change | cash generation trend | yoy_lag4 | directional after P0 fixes |
| beneish_m_score (TTM/annual only) | manipulation screen | annual basis | literature screen-only; FP economics known |
| receivables_growth_spread | revenue pulled forward | yoy_lag4 | near-zero measured signal — reduced weight, single receivables signal |
| inventory_growth_spread | demand softness masked | yoy_lag4 | season-validated flag (AAPL benign case handled via benign_candidates) |
| capex family (growth spread, /D&A, regime shift) | capex outrunning revenue | windowed | right-signed on all outcomes (strongest family) |
| net_debt_to_ttm_ebitda + interest_coverage + current_ratio + debt_to_assets + leverage_change | balance-sheet stress | TTM/stock | right-signed (rate-cycle caveat) |
| dilution (one signal: diluted_share_growth) | shareholder erosion | yoy | season-validated as evidence; scored descriptively |
| issuance_pressure | funding dependence | TTM | right-signed |

### Funding/liquidity (evidence plane)
grants-receivable delta vs NI · supplier-finance SFPO/AP + trend · ITC/prepayment context · covenant-text presence (from survey: violation text > ratio).

### Capital markets (evidence plane)
S-3 shelf (early warning) · 424B takedowns (equity/debt split, primary/secondary, deal-vs-current price) · ATM detection (8-K Item 1.01 keywords; utilization NOT promised) · ≥50%-secondary flag · lockup dates (ceilings, never countdowns).

### Insiders (evidence plane)
cluster purchases · derived late Form 4 · 10b5-1 plan terminations · Form 144 plan-adoption timing. Never framed as accusation; plan sales never framed as suspicious.

### Filing behavior (evidence plane)
NT 12b-25 (+extension arithmetic) · 4.02 · 4.01 (resign vs dismiss) · 2.06 · filing-lag drift · same-day-10-Q speed advantage noted per filer.

### Narrative/disclosure (evidence plane)
high-severity emergence (validated) · adjustment ledger + n-gram novelty · risk-factor ADD/REMOVE + specificity · MD&A numbers-to-words change · decomposed volume change · KPI removals.

### Consistency (evidence plane)
4 existing mismatch templates · capital-integrity↔offerings · guidance↔actuals.

**Retirements** (from scoring; some retained as labeled evidence): adjustment_recurrence pair, beneish_tata/dsri standalone, working_capital_swing_to_income, buyback_offset_ratio, sbc_to_revenue/sbc_to_cfo (→evidence), guidance_shift, defensive_tone_change (→evidence, "unvalidated" label), asset_quality_proxy, intangibles_to_assets (→evidence), deferred_revenue_growth_spread (→evidence until validated), incremental_revenue_per_capex (→evidence), net_share_count_change (merged), dso/dio trends replaced by seasonal-comparator versions.

Result: **~17 scored signals** (from ~35) representing genuinely distinct economics, plus ~20 evidence streams.

---

## 6. Provenance rules (non-negotiable)

1. Every fact the user sees traces to `{accession, form, filed_date}` and, for XBRL, the tag; for text, the excerpt.
2. Every report and card carries `data_as_of` + per-source fetch stamps.
3. Derived values cite method + version (`method: ytd_diff@mapper-2.1`).
4. Acquisition failures are first-class items ("could not fetch X") — never conflated with absence.
5. An evidence item without provenance fails validation — enforced in schema and CI, not convention.

## 7. Triage layer — what replaces the composite

The composite score is **removed from all user surfaces**. (Kept internally only if the wide-sweep needs a sort key.) Rationale: measured non-discrimination; seven structural causes (assessment §3.2); and the gap-research finding that **no validated forensic/distress score aggregates by weighted-averaging dozens of subscores** — the ESG-composite literature (Berg–Kölbel–Rigobon 2022) shows that design destroys discrimination regardless of weights. See [gap_research_2026Q3.md](gap_research_2026Q3.md) Gap 1.

Replacement — three separately-stated readouts, no cross-plane blending:

1. **Distress thermometer**: balance-sheet/cash-flow stress expressed as own-history + same-year percentiles, aggregated **average-within-cluster, max-across-clusters** (AOM — the outlier-ensemble result: averaging buries the one screaming detector). Non-computable ratios become **Ohlson-style regime dummies that ADD concern** (`RATIO_REGIME_BROKEN`: NI<0, NI<0 ×2, EBITDA<0, equity<0) instead of renormalizing away (fixes P0-9's inversion with 45 years of O-score precedent); refusal-prone ratios re-denominated to assets-scaled forms (Altman/CHS convention) so they stay computable through zero.
2. **Tiered flag counts** (Piotroski form — the aggregation that survives at small n): Tier-1 (validated, low-FP: high-severity emergence, 4.02/NT/auditor events, restatement footprint, missed-extended-deadline) · Tier-2 (directional: accrual/capex/leverage changes vs reference class) · Tier-3 (context). Flag thresholds at cross-sectional extremes once L5 exists; hand-set until then, labeled as such.
3. **Change summary**: NEW/WORSENED/CLEARED item counts from L4.

Upgrade path, evidence-gated: cross-sectional **conformal p-values** per cluster (distribution-free, no labels needed) once the L5 reference store exists; **Dechow-style relative-risk language** ("N× the base rate") only where a survey-verified base rate applies. Weighted averaging survives only as a tie-breaker within a flag-count band. No 0–100 headline. Direction words ("Mixed") retired.

## 8. Migration path (evolutionary)

1. P0 correctness fixes inside current structure (no architecture change needed).
2. `SourcedValue` + provenance plumbing (schema change, mechanical).
3. Comparator service + TTM constructor (new module; formulas opt in signal-by-signal).
4. Offerings→pipeline; 4.02/NT promotion; diagnostics→report.
5. Ledger unification; card surface; composite retirement.
6. Vintage store; reference-class distributions; insider ingestion.
7. Journal schema upgrade (independent of all the above; can start immediately).

Each step keeps the golden tests green (with intentional, reviewed diffs where output changes).
