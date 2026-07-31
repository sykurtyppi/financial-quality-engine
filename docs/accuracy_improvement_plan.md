# Accuracy Improvement Plan — researched 2026-07-31

Every item maps to a measured failure or a manual discovery from the Q2 2026 live
season ([season_2026Q2_engine_performance.md](season_2026Q2_engine_performance.md)).
**Constraint honored throughout:** the modeling phase stays concluded — nothing here
adds a predictive detector or touches score weights. Fixes are correctness repairs;
new streams are *evidence/context* (plane 1 facts surfaced in reports and the change
model), never score inputs.

---

## A. Fix the measured artifacts (code-only, highest priority)

### A1. Replace `adjustment_recurrence` keyword matching with novelty detection
**Failure:** fired 79–88/100 on 11/11 companies; hand-discounted 11/11 times.
**Fix, academically grounded:** fire on *changed* language, not *present* language.
- Brown & Tucker (2011, *JAR* 49(2)) — cosine-similarity measure of year-over-year
  MD&A modification; modifications track real operational change and the market
  reacts to them.
- Dyer, Lang & Stice-Lawrence (2017, *JAE*) — "stickiness" via identical 8-word
  phrases vs the prior year's filing; documents secular growth of boilerplate
  (which is exactly why presence-matching can't work).
**Implementation (deterministic, no ML):** for each adjustment term hit, check
whether its containing sentence shares an 8-word phrase with the prior-year filing.
Sticky → suppress (boilerplate). Novel → flag with the excerpt. Cross-sectional
backstop: a term/phrase appearing in >80% of a reference corpus of filings is
boilerplate by definition (IDF logic). This converts the season's worst detector
into the engine's most Lazy-Prices-aligned one.

### A2. `net_debt_to_ebitda` on TTM EBITDA
**Failure:** GLW flagged 7.39x (top red flag, 90/100); true ~1.85x. Quarterly
denominator overstates ~4x for normal earners. **Fix:** trailing-4Q EBIT + D&A when
≥4 quarters exist; label "quarterly basis, unreliable" otherwise; cross-check
against interest coverage and suppress the flag when coverage >5x.

### A3. Seasonal basis for flow metrics
**Failure:** GLW Q1 FCF trough (0.7%) read as deterioration; Q2 printed ~30%.
**Fix:** `fcf_margin_trend`, `cfo_to_net_income` trend, and DSO trend compare
same-fiscal-quarter-prior-year first, trailing mean second; disagreement → report
both with the seasonal caveat. (MSFT's June-quarter receivables spike: same class.)

### A4. Demote the composite score
**Failure:** 11 runs clustered 31–58 "Mixed"; zero observed discrimination (cleanest
score fell 6%, worst hyperscaler rallied 10%). **Fix:** composite becomes an
internal sort key; reports lead with evidence-linked findings and §5 deltas — the
layout already specified in PORTFOLIO_MANAGER_EXPERIENCE.md §2/§9.

## B. New evidence streams (all free; all context, never score inputs)

### B1. Offering-cadence + use-of-proceeds reader — fixes the season's worst miss
**Failure:** FPS Capital Integrity scored 10/100 (lowest concern) while the sponsor
sold four times in five months; found only by hand-reading the filing index.
**Source:** EDGAR submissions API (already ingested) — form types S-1/S-3/S-3ASR/
424B4/424B5/S-1MEF + 424 use-of-proceeds text ("selling stockholder", "we will not
receive any of the proceeds", price per share).
**Output:** offerings timeline per company; primary vs secondary split; deal price
vs current price (the KTOS $84-vs-$46 fact, automated). Cheapest high-value item
on this list — it is filing-index parsing plus three regexes.

### B2. Form 4 insider stream with routine/opportunistic classification
**Manual win to automate:** the GLW "CEO+SVPs sold $30.7M, zero buys, −56% drawdown"
finding. **Grounding:** Cohen, Malloy & Pomorski, "Decoding Inside Information"
(*Journal of Finance*; NBER w16454): essentially all predictive content of insider
trades sits in *opportunistic* (irregularly timed) trades (~82bp/mo VW abnormal);
*routine* trades (same-calendar-month pattern in ≥3 prior years) carry ~zero.
**Implementation:** EDGAR Form 3/4/5 (free; `edgartools` parses them); deterministic
routine/opportunistic rule per the paper; presented as evidence lines ("no insider
purchases during a 56% drawdown; sales were routine-classified"). Caveat 10b5-1
plans in the output.

### B3. 8-K item-code event stream
The engine already runs 4.02 forensics in the restatement control; generalize:
map every 8-K to its item codes (1.01 material agreements, 2.03 new debt, 4.01
auditor change, 4.02 non-reliance, 5.02 officer departures) and surface as a dated
event timeline in reports. Free, structured, already-ingested filings.

### B4. USAspending.gov award verification (defense/government names)
**Manual win to automate:** decomposing KTOS's "$692M of wins" into firm obligations
vs IDIQ ceilings. The USAspending API is public, free, no key, covering contracts/
grants back to FY2008 with recipient, amount, agency, and dates. For any issuer
with government concentration, reported "awards" can be cross-checked against
actual obligations — turning press-release backlog claims into verifiable data.
(Directly relevant to KTOS and AMPX before their prints next week.)

### B5. FINRA short interest (context plane)
Twice-monthly, free; a pending FINRA rule (4321 amendment process, 2026) may make
it weekly. Squeeze/crowding context for the positioning notes the reports already
carry. Low priority; do after B1–B4.

**Deliberately skipped:** earnings-call transcripts (no reliable free source; 403s
everywhere), 13F clones (45-day lag, noise — measured this season), consensus
estimates (licensed), anything predictive (gate stands).

## C. Funding-context pass — fixes the AMKR-class FCF misread
When an FCF/cash-conversion flag fires, run a deterministic text search over the
same filings for grant/ITC/prepayment/customer-deposit language and attach the
excerpts as *benign-explanation candidates* beside the flag (never suppressing it).
The change model's "strongest benign explanation" field gets populated from the
filer's own words.

## Priority order (impact ÷ effort, from season evidence)

1. **B1** offering cadence (worst miss; trivial build)
2. **A1** novelty-based adjustment detector (worst FP; medium build)
3. **A2 + A3** denominator/seasonality fixes (small builds)
4. **C** funding-context pass (small; big interpretive payoff)
5. **B2** insider stream (medium; high evidence value)
6. **B4** USAspending (small; portfolio-relevant now — KTOS/AMPX report next week)
7. **B3** 8-K events (small)
8. **A4** score demotion (bundled with the change-card frontend, phases 5–6)
9. **B5** short interest (later)

All of A and C plus B1 are ungated correctness/evidence work under the existing
architecture plan. B2–B4 add ingestion but no modeling — they extend plane 1.
