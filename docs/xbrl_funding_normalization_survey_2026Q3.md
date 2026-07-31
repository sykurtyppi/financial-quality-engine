# Survey 5: XBRL quality, non-operating funding, quarterly normalization
*(agent-researched 2026-07-31; fifth companion survey. [VERIFIED] = the agent
confirmed against primary data/documents in-session — several findings were
proven live against SEC APIs, not just cited.)*

## Structural findings about our own data path [VERIFIED]

- **`companyfacts` is insufficient for two of three threads.** It excludes
  custom taxonomies (so grant/supplier-finance extensions are invisible),
  `decimals`, dimensions/segments, and the calculation linkbase. `fy`/`fp`
  describe the FILING, not the fact (the same fact appears under multiple
  fy/fp as comparatives → keying on them double-counts). `frames` returns the
  latest vintage only and drops non-calendar filers.
- **Financial Statement Data Sets (FSDS)** fill most gaps: `TAG.custom`,
  `TAG.crdr` (natural sign), `NUM.qtrs`, `NUM.segments`, `SUB.prevrpt`,
  `PRE.stmt` (face-vs-footnote). 127.8 MB/quarter, free, verified live.
  → a second ingest path is required for the funding/extension work.
- **✓ ENGINE AUDIT PASSED — YTD differencing.** The survey verified on Apple
  that ALL cash-flow facts are YTD-only ({Q1, 6M, 9M, FY} durations; `fp` has
  no Q4; discrete-quarter 10-K facts vanished after FY2020 with Item 302(a)'s
  elimination) — a mapper treating YTD facts as quarters would overstate CFO
  ~2.6x. **Checked 2026-07-31: `companyfacts_mapper` already handles every
  trap** — duration classification, YTD differencing (`ytd_diff`), Q4
  derivation (`fy_minus_3q`), latest-filed-wins keyed on `(start, end)` not
  fy/fp, 52/53-week awareness, per-value provenance methods. The season's CFO
  figures were correctly differenced. Remaining nice-to-have: add the Apple
  FY2024 YTD fixture as a regression test.

## Vintage/restatement detection — proven live [VERIFIED]

- companyfacts supports it: group by `(tag, unit, start, end)`; ≥2 values from
  different accessions = revision. **Kraft Heinz Big R detected cleanly**
  (GrossProfit FY2017 −6.9% across vintages).
- Base rate: 3.4% (AAPL) – 6.7% (KHC) of period-groups change, mostly benign
  (standard adoptions, reclassifications) → **require corroboration**: Item
  4.02 8-K, `/A` form, or `SUB.prevrpt=TRUE`; larger threshold for suspected
  little r.
- **Little r restatements** (~12% of firms, Tan & Young 2015) are invisible to
  4.02 monitoring but visible in the vintage diff; Choudhary et al. (CAR 2021):
  immaterial corrections are a leading indicator of future material errors,
  ICFR weaknesses, comment letters. **The best academic justification yet for
  the PIT vintage store (phase 2).** PCAOB: Big R ~3%/yr; 29% of Big R firms
  changed auditor in the prior year vs 11% baseline (→ pair with 4.01 stream).
- **DQC rules are reimplementable with attribution** (202 rules on GitHub,
  human-readable specs; license permits derivative implementation). Start:
  sign rules (DQC_0015), balance-sheet identity (0004), period validity
  (0146), percentage scale (0091) — filing-hygiene evidence.

## Non-operating funding — failure 3 finally closed, with live pipelines

- **Supplier finance (ASC 405-50): runs end-to-end TODAY on companyfacts.**
  [VERIFIED live]: P&G SFPO/AP **37.9%**, General Mills **37.5%**, Campbell's
  18.6%, Sherwin-Williams 7.6%. Rules: (A) SFPO/AP >25–30% flag;
  (B) recompute leverage with SFPO as debt, report delta; (C) DPO trend —
  rising DPO + rising SFPO = the Carillion signature; (D) rollforward vs
  IncreaseDecreaseInAccountsPayable divergence = CFO flattered by program
  growth; (E) filers using custom "ProceedsFromStructuredPayables"-type
  financing tags have conceded the debt characterization (FSDS path).
  Caveats: ~2yr tag history; text-first (315 10-K mentions vs ~7–17 tagged);
  reclassification is OUR overlay, not GAAP — label it.
- **Government grants: `GrantsReceivable{Current,Noncurrent}` is the real
  detector** (standard tag, in companyfacts, n≈48: Micron $809M CHIPS, First
  Solar ~$625M §45X). ASC 832's GovernmentAssistance* family is essentially
  unused (n=1–2) vs 348 text mentions → text-primary, tags-confirm.
  First Solar pattern [VERIFIED]: credit income recognized well before cash —
  flag when Δgrants-receivable is a large fraction of net income (the accrual
  construction applied to funding). **Gross margin is not comparable across
  IRA beneficiaries** (contra-COGS vs other-income vs tax-line presentation;
  diversity persists ≥3 more years — ASU 2025-10 effective FY2029).
- **Deferred revenue as funding:** ΔContractWithCustomerLiability/revenue —
  direction replicated (US/China; Prakash & Sinha 2013: current deferrals
  depress current margin, raise future margin; analysts underestimate) but
  **zero effect sizes retrieved; pre-ASC-606 tagging** — direction only.
- **Securitization**: ProceedsFromAccountsReceivableSecuritization (n=40)
  scaled by revenue vs CFO change. Factoring: customs only.
- **USAspending→CIK: DOWNGRADE improvement-plan B4.** No deterministic
  crosswalk exists; the canonical peer-reviewed method (Samuels, TAR 2021) is
  fuzzy name-matching + manual inspection; UEI is per-registration, not
  per-entity; SEC publishes no UEI/DUNS. → low-confidence corroborating
  evidence only, never a finding trigger; require name match vs Exhibit 21
  subsidiaries + former names.

## Quarterly normalization — citations for A3, now verified

- **Foster (1977) Table 2 read from the original:** seasonally-differenced
  quarterly earnings autocorrelations r₁=.445, r₂=.244, r₃=.128, **r₄=−.121**;
  and QoQ leaves seasonal residue (r₄=.408 in first differences). → lag-4
  (same-quarter-prior-year) is the baseline for every quarterly flow;
  optional drift adjustment (mean seasonal difference, ≥8 quarters).
  Cite Foster for numbers; Bernard & Thomas for qualitative pattern ONLY
  (magnitudes unverified — do not ship them).
- **Q4 is structurally anomalous — Binz & Kapons (2025 WP, PDF-read):** Q4
  asset-scaled earnings −47.4% vs interim quarters (median gap larger);
  effect entirely expense-driven (bad debt, warranty, inventory impairments);
  **CFO moves the OPPOSITE way in Q4** while accruals fall — so Q4-
  concentrated CFO−NI divergence is a deterministic signature, and normal-Q4
  thresholds must be calibrated to Q4 baselines. Mechanism: ASC 270 integral
  method + only annual figures audited + Q4 is always arithmetically derived.
  **Never compare Q4 to Q1–Q3; only Q4 to Q4.**
- **TTM type guard:** 668 of ~2,000 sampled credit-agreement leverage
  covenants use four-quarter EBITDA; none use single-quarter. Encode: any
  instant/duration ratio must use TTM denominator or refuse to emit. The
  "~4x overstatement" is arithmetic — cite no paper. Never label unadjusted
  TTM EBITDA "Consolidated EBITDA."
- **52/53-week calendars [VERIFIED on Apple]:** FY2023 = 370 days, fiscal Q1
  = 97 days. Flag FY ≥369 days / quarter ≥96 days; annotate (never silently
  adjust) YoY comparisons; do not assert a "2%" impact. Fiscal-year-end
  changes (1,786 firms, 1993–2008): suppress YoY across the transition.

## Custom-tag question — resolved as evidence-only

Four papers, three directions (bad-controls / better-disclosure / both-by-
location / neither-just-cost). If shipped at all: **face-of-statement
extension rate, relative to own prior year and size/filer-status cohort**
(PRE.stmt enables the split), presented as evidence citing all four. Never
scored. DERA trend page has no extractable numbers (chart images).

## Do-not-ship list (load-bearing)

Bernard–Thomas autocorrelation magnitudes · "~2%" 53rd-week impact · any
"4x leverage" citation · deferred-revenue effect sizes · Kerstein & Rai
(not found as described) · Fitch/Moody's numbers (secondary reporting).
