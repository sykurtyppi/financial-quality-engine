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

---

## Addendum (2026-07-31): literature-survey merge

The agent survey ([arxiv_survey_2026Q3.md](arxiv_survey_2026Q3.md)) upgrades this
plan in four places and vindicates one decision:

1. **A1 is upgraded.** The better fix for `adjustment_recurrence` is not novelty
   detection alone but the **per-issuer non-GAAP adjustment ledger** (Doyle/Lundholm/
   Soliman composition result): track named exclusion labels + dollar amounts from
   reconciliation tables; fire on recurrence streaks and cumulative one-time charges
   vs cumulative operating income. Novelty gating (survey #3) becomes the general
   text-detector gate, with segmentation sanity checks (#12) beneath it — part of
   the season's 11/11 may have been section-boundary artifacts, not detector logic.
2. **A2 is superseded where disclosure exists.** Covenant-violation/waiver phrase
   detection (Nini/Smith/Sufi) replaces the leverage-ratio flag with a disclosed
   fact; the TTM denominator fix remains for filers with no covenant disclosure.
3. **New evidence stream ranked above B2–B5: SEC comment letters (UPLOAD/CORRESP)**
   — free, rare, high-specificity, regulator-authored, and directly in the
   thesis-monitor frame. Also new: risk-factor ADD/REMOVE set-differencing
   (Lyle/Riedl/Siano) strictly improves the existing word-count detector, and
   NT 10-K/10-Q reason grading is a cheap high-signal event.
4. **Presentation policy gains a citation.** Beneish & Vorst (2022): >100:1
   false-to-true-positive ratios in the best published misstatement models —
   the quantitative justification for the frozen composite and for per-finding
   base-rate disclosure.

**Survey gaps to respect:** failures 3 (non-operating funding) and 4 (sponsor
sell-downs) have no verified literature yet — B1 (shipped) and C proceed as
engineering, not as paper-backed rules; commission focused research before
extending them. XBRL custom-tag rate: neutral context only, flag changes only.

**Revised do-first:** (1) adjustment ledger, (2) n-gram gates + segmentation
checks, (3) comment-letter stream, then the prior queue.

## Addendum 2 (2026-07-31): all four survey threads delivered

Three further companion surveys landed after the first merge:
[calibration_journal_survey_2026Q3.md](calibration_journal_survey_2026Q3.md),
[textual_measures_survey_2026Q3.md](textual_measures_survey_2026Q3.md),
[deterministic_components_survey_2026Q3.md](deterministic_components_survey_2026Q3.md).
What they change:

1. **The journal itself gets a schema upgrade (evidence-based).** Bare
   pre-registration does nothing (Brodeur et al. 2024); specificity is the
   active ingredient across three literatures. BEFORE blocks move to
   machine-checkable five-field assumptions (metric/threshold/window/source/
   resolution-date) + required falsifier/confirmer fields (Arkes 1988 port)
   + always-resolve-including-abandoned. Conviction input should not stay
   coarse 1–5 (Mellers 2015 granularity correlate). NULL RESULT preserved:
   no literature shows decision journals improve outcomes — never claim it.
2. **Best-evidenced new feature anywhere: reference-class base rates from
   XBRL peer sets** (Chang et al. 2016: comparison classes Brier 0.17 vs
   0.49 control). Medium effort.
3. **Honest value framing: the trades it prevents** (Barber & Odean gross/net
   decomposition) — alerts must show "assumption NOT violated" as prominently
   as violations.
4. **Segmentation is a measured ~10% error floor under every text detector**
   (rule-based 0.909 precision unconditioned; the public 3,737-filing
   benchmark becomes our validation harness). Same-year cross-sectional
   benchmarking (Brown & Tucker) suppresses standard-change FPs.
5. **Cheap 8-K/NT wins added to B3:** 12b-25 + extension-date arithmetic,
   NT→4.02 30-day join (the SEC's own heuristic), 4.01 resignation/dismissal
   split, 8.01-is-not-boilerplate, 2.06-absence≠no-impairment.
6. **Design principles now cited, not asserted:** change > level; density >
   presence (Diction 70–83% misclassification precedent); ruleset recall
   ceiling ~60–75% of filer behavior (FNXL tails) — disclose it.
7. **Licensing ledger opened** (GPL-3.0 edgar-crawler, LM commercial license,
   non-commercial datasets) — resolve before productization.
8. **Lazy Prices corrected at the source:** headline 34–58bp/mo VW; 188bp is
   Risk-Factors-only; 86% of changes negative → sign diffs with LM lists;
   drop tables >15% numeric; cue-absence = highest-value alerts.

**Consolidated do-first (supersedes prior orderings):**
(1) adjustment ledger + XBRL special-item recurrence counter;
(2) segmentation harness + n-gram gates + same-year benchmarking;
(3) NT/4.02/4.01 8-K rules; (4) comment-letter stream;
(5) journal schema upgrade; (6) reference-class base rates.

## Addendum 3 (2026-07-31): survey 5 — XBRL quality, funding, normalization

[xbrl_funding_normalization_survey_2026Q3.md](xbrl_funding_normalization_survey_2026Q3.md)
closes the two threads every prior survey flagged unresearched, with live-verified
pipelines. Plan changes:

1. **C (funding-context pass) is now grounded and partly buildable TODAY:**
   supplier-finance rules A–D run on companyfacts (P&G SFPO/AP 37.9% verified
   live); `GrantsReceivable` is the working grant detector (n≈48; First Solar
   income-before-cash pattern); text-primary/tags-confirm design (315 vs ~7–17).
   Gross margin non-comparability across IRA beneficiaries is a new finding class.
2. **B4 (USAspending) DOWNGRADED:** no deterministic CIK crosswalk exists; the
   canonical method is fuzzy+manual (Samuels TAR 2021). Corroborating evidence
   only, never a trigger; require Exhibit-21/former-names name match.
3. **Phase-2 PIT store gains its academic backbone:** vintage diffing proven
   live (Kraft Heinz); little r revisions (~12% of firms) are invisible to 4.02
   monitoring but visible in vintage diffs and predict future reliability
   problems (Choudhary et al. CAR 2021). Corroboration triggers: 4.02, /A,
   SUB.prevrpt. Base rate 3–7% of period-groups, mostly benign — threshold+
   corroborate, never raw.
4. **A3 gets verified citations and two new rules:** Foster (1977) lag-4
   baseline (r₄=−.121 read from the original); Binz-Kapons Q4 anomaly (−47.4%
   mean, accrual-driven, CFO opposite-signed) → never compare Q4 to interim
   quarters; Q4-concentrated CFO−NI divergence as a deterministic signature;
   53-week/fiscal-change annotations.
5. **A2 type guard confirmed by contract evidence** (668/2000 covenants use
   4-quarter EBITDA; zero single-quarter). Cite no paper for the "4x" — it's
   arithmetic.
6. **New ingest path required:** FSDS (TAG.custom/crdr, NUM.qtrs/segments,
   SUB.prevrpt, PRE.stmt) for extensions, custom funding tags, and
   face-vs-footnote splits. DQC rules (202, license permits reimplementation
   with attribution) as filing-hygiene evidence.
7. **Mapper audit PASSED** — YTD differencing/Q4 derivation/(start,end) keying
   already implemented; add the Apple FY2024 fixture as a regression test.
8. **Do-not-ship list adopted** (B&T magnitudes, 53rd-week "2%", "4x" citation,
   deferred-revenue effect sizes).

## Addendum 4 (2026-07-31): survey 6 — insiders and offerings (program complete)

[insider_offerings_survey_2026Q3.md](insider_offerings_survey_2026Q3.md) closes
the last two flagged gaps (insider classification; sponsor sell-downs/offerings).
Plan changes:

1. **B2 (insider stream) restructured.** CMP routine/opportunistic stays but
   with decay expectations (~60–70%, sell side dead) and 10b5-1-plan dedupe.
   Two better cheap signals added ABOVE it: **cluster purchases** (≥2 insiders,
   same/consecutive days, code P only — orthogonal to CMP, small build) and
   **derived late Form 4** (>2 business days; measured base rate 8.71%;
   never use `transactionTimeliness` — 99.5% blank). Plus the pre-8-K
   disclosed-before/after sign-flip and the free pre-QEA window flag.
2. **10b5-1 sub-stream added:** checkbox boolean lexical trap (== "1" drops
   ~25%); Form 144 structured planAdoptionDate as early warning (fires at
   order placement); Item 408 ecd: tags quarterly; **plan terminations are
   the one surviving signal**; red flags reframed as compliance anomalies.
   Regulatory date guards encoded (Apr 1 2023 structural break, et al.).
3. **B1 (shipped reader) validated + extended:** >50%-secondary = academically
   unusual (Billett screen) — our "supply without balance-sheet benefit" flag
   gains a benchmark; add ATM-program detection (8-K Item 1.01 keyword set +
   424B5 capacity; utilization vs 0.43/0.27); S-3 filing = early warning,
   424B5 = fait accompli; lockup dates rendered as ceilings, never countdowns;
   direct listings = no-lockup negative rule; PIPE floating-conversion terms
   with d/(1−d) arithmetic, no return claims (contested).
4. **Share-count reconciliation demoted to data-quality check** (no literature;
   original heuristic, label as such); share-count GROWTH retained as
   dilution evidence (survives the microcap critique). Verified traps:
   cover-vs-balance-sheet date gaps, per-class axes, splits,
   StockIssuedDuringPeriodSharesNewIssues sparsity.
5. **Program-wide editorial adopted:** surface facts (float expansion,
   dilution arithmetic, filing lag, cluster count) and link filing patterns
   to governance/accounting risk, never to returns — the return effects have
   decayed, been legislated away, or been challenged nearly everywhere.

**The six-survey research program is complete.** All gaps flagged in surveys
1–5 are now either closed with verified findings or explicitly marked
do-not-build. Final consolidated Tier-1 build queue (cheap, evidenced, no
return claims): adjustment ledger · segmentation harness + n-gram gates ·
supplier-finance rules A–D · GrantsReceivable detector · cluster purchases ·
derived late Form 4 · NT/4.02/4.01 rules · ATM detection · comment letters ·
journal schema upgrade · reference-class base rates.
