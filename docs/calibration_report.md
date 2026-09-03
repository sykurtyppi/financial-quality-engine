# Calibration Report (v0.4 regeneration)

Date: 2026-09-03 · Config version: 0.4.0 · Artifact: `data/backtest/backtest_results.csv`
Harness: `scripts/run_backtest.py` → `scripts/analyze_backtest.py` · `scripts/validate_thermometer.py`

> **Regenerated 2026-09-03.** The previous artifact (2026-07-03, config 0.3.0)
> carried a known defect: forward-outcome columns anchored on the last *ended*
> quarter while scores anchored on the last *filed* quarter, with a positional
> 4-quarter lookahead and no contiguity guard. That fix
> (`app/services/backtesting/outcomes.py`) is now in the artifact — but so is
> everything else that landed between July and September: the P0 TTM/YoY
> bases, fourteen retired components, and the round 10–15 denominator and
> capex guards. **Every number below therefore reflects the current scoring
> code, not the anchor fix in isolation.** The isolated effect of the anchor
> fix is small and is quantified in the changelog at the end.

## Read this first

This is a **signal validity audit on free data**, not a statistically
validated calibration. It is sufficient to (a) demote components that are
demonstrably wrong-signed, (b) promote blocks with consistent right-signed
evidence, and (c) fix the score bands to the real distribution. It is NOT
sufficient to claim predictive power for the overall score, and this report
does not claim it. The composite has since been retired from every product
surface (P1-C); it is retained in this artifact as a comparison baseline for
the thermometer.

## Methodology

- **Universe**: 75 companies, stratified: hypergrowth SaaS, capex-heavy AI/
  infrastructure, cyclicals, serial acquirers, energy, banks (exclusion-path
  check), staples/healthcare/retail/tech controls, and 7 known ex-post stress
  cases (SMCI, PTON, CVNA, OPEN, BYND, LCID, W) as a qualitative validation set.
- **Walk-forward**: as-of dates = calendar quarter ends 2021Q1–2025Q1 + 75-day
  filing lag → 1,275 company-quarter rows; 1,146 scored (`ok`), 119 excluded
  financials, 10 skips (stale/short history). 25 `ok` rows withhold the
  overall score because less than 50% of block weight had data (PEP 9, DVN 7,
  TWLO 4, MRK 4, OXY 1) — a coverage consequence of the component retirements,
  up from 5 rows in the July artifact.
- **Point-in-time**: signals computed ONLY from facts with `filed` ≤ as-of
  (companyfacts filing dates). Facts without a filed date are dropped in PIT
  mode. Outcomes may see the future; signals cannot.
- **Entity pinning**: the SEC ticker registry now maps XOM to a 2026
  holding-company successor with no pre-2026 facts; the universe pins XOM to
  its historical CIK (34088) so the walk-forward sees the real history.
- **Outcomes**: 3/6/12-month SPY-relative returns (dividend-adjusted closes);
  realized forward operating-margin change (t+4 vs t); forward-vs-trailing
  4-quarter FCF-margin change; forward 4-quarter net-income growth; 8-K Item
  4.02 (non-reliance) within 24 months. Forward outcomes now anchor on the
  same filed quarter as the signal and require five contiguous quarterly
  periods (75–105-day gaps); an interrupted history yields no outcome rather
  than a wrong one.
- **Proxies**: analyst-estimate revisions are not freely available, so
  "earnings revisions" and "FCF surprises" are proxied by realized changes and
  labeled as such throughout.

## Known biases (unavoidable at this data tier)

1. **Survivorship**: universe drawn from currently listed tickers. Collapsed
   frauds are absent → true-positive rates are understated, false-positive
   rates overstated. This is the single largest limitation.
2. **Period specificity**: 2021–2025 contains the 2022 rate shock; leverage
   metrics' return signal is partly a rate-cycle effect and may not generalize.
3. **Scale**: ~70 scored names is enough for direction, not for significance
   testing; no IC standard errors are quoted because clustered inference on
   overlapping 12-month windows would be false precision at this n.
4. **Narrative Drift block untestable**: no historical document corpus in the
   backtest; its weight remains fully uncalibrated.

## Results

### Overall score (retained as baseline only)

Empirical distribution (n=1,121 with returns): min 16.6, p50 28.7, p80 37.1,
p90 42.7, max 72.9 — the score uses roughly a third of its nominal 0–100
range, and the whole distribution sits ~3 points lower than in July (mean
delta −2.4 across the 1,112 rows scored in both artifacts).

Quintiles vs forward 12M SPY-relative return:

| Quintile (score range) | Mean rel. 12M | Median rel. 12M | n |
|---|---|---|---|
| Q1 (17–24) | −0.9% | −5.8% | 224 |
| Q2 (24–27) | +2.0% | −4.9% | 224 |
| Q3 (27–30) | +12.6% | −2.2% | 224 |
| Q4 (30–37) | +3.4% | −10.8% | 224 |
| Q5 (37–73) | +6.9% | −9.4% | 225 |

Interpretation: **the July finding that separation existed in the top
quintile no longer holds cleanly.** Q4 and Q5 have the worst medians, but Q5's
mean is positive (a few large rallies), and Q1–Q3 are not monotone. The
composite is at best a weak tail screen; this is consistent with the live
2026Q2 season, where it discriminated in neither direction, and it is why
the composite was retired from every surface.

At the empirical p80 threshold (score ≥ 37.1):
- **Hit rate** (12M rel. return < −10%): **50.0%** vs base rate 45.3% (+4.7 pp;
  July: +9.8 pp at its p80 of 40.3)
- **False-positive rate among flagged: 50.0%** — half of flags see benign
  forward returns. At the July threshold of 40.3 the hit rate is 55.0% on 151
  flags, i.e. the lift is concentrated further into the tail than before.
- Margin-deterioration hit rate at p80: 26.5% vs base 26.8% — the *overall*
  score adds nothing for margins (the Earnings Quality *block* does; see below).

The legacy >60 "negative" band now fires on 10 rows (July: 1) with an 80% hit
rate — all stress cases. Direction bands remain positive < 32 / negative > 45
in config; against the regenerated distribution those sit at ~p62 and ~p92
rather than the p50/p90 they were anchored to. Re-anchoring is a config +
snapshot change and is deliberately left for a reviewed follow-up.

### Block-level signal (Spearman IC of concern vs outcome; negative = works)

| Block | vs 12M rel. return | vs fwd op-margin chg | vs fwd FCF-margin chg | Verdict |
|---|---|---|---|---|
| Earnings Quality | +0.06 | **−0.17** | +0.04 | Margin-deterioration signal confirmed (classic accrual result); no return signal |
| Cash Conversion | **−0.11** | +0.12 | +0.30* | Return signal confirmed; *FCF wrong-sign is mean reversion (bad FCF now → base-effect improvement), stronger than in July |
| Capex Discipline | −0.03 | **−0.07** | **−0.16** | Fundamentals signal intact; the July return signal (−0.11) did not survive the capex guards |
| Balance Sheet Stress | **−0.17** | +0.24* | +0.25* | Return signal strengthened (from −0.07); *wrong-signed on fundamentals — leverage concern precedes base-effect recovery in this rate cycle |
| Revenue Quality | +0.02 | +0.06 | +0.08 | Near-zero measured signal |
| Working Capital Stress | −0.01 | +0.04 | +0.07 | No signal as constructed |
| Capital Integrity | −0.05 | **+0.17 (wrong sign)** | **+0.21 (wrong sign)** | Still broken as a *predictor* (less so than July's +0.31/+0.25); n fell to ~550–650 with the SBC/buyback retirements |
| Narrative Drift | — | — | — | Untestable (no documents) |

Strongest right-signed components vs 12M returns: net_debt_to_ebitda
(−0.26), interest_coverage (−0.18), fcf_margin (−0.17), issuance_pressure
(−0.13), debt_to_assets (−0.10), cfo_to_net_income (−0.08).
Strongest predictors of forward margin deterioration: capex_to_da (−0.17),
total_accruals and accrual_trend (−0.15 each), current_ratio (−0.11),
debt_to_assets (−0.11).

**Why Capital Integrity is wrong-signed**: in 2021–2025, high dilution
concern concentrated in hypergrowth software that subsequently *improved*
margins (scale leverage + the 2023–24 efficiency wave). Dilution is a real
shareholder cost, but in this universe it behaved as a growth-stage marker,
not a deterioration predictor. The block is retained as *descriptive*
information at reduced weight.

**Why Balance Sheet is wrong-signed on fundamentals but right-signed on
returns**: leverage concern in 2021–22 flagged the names the 2022 rate shock
punished (returns), while their subsequent margin/FCF changes were base-effect
recoveries (fundamentals). Both readings are period-specific (bias 2).

### False-positive diagnostics by archetype (score ≥ p80 = 37.1)

| Archetype | Scored rows | Mean score | Flag rate | FP rate among flagged |
|---|---|---|---|---|
| stress_case | 98 | 45.0 | 74.5% | **32.9%** |
| hypergrowth_saas | 160 | 31.9 | 24.4% | 38.5% |
| control_healthcare | 81 | 30.3 | 13.6% | 63.6% |
| control_retail | 119 | 30.1 | 17.6% | 57.1% |
| control_tech | 136 | 29.7 | 19.1% | 65.4% |
| cyclical | 153 | 29.4 | 15.7% | 62.5% |
| control_staples | 76 | 28.8 | 6.6% | 80.0% |
| capex_ai_infra | 102 | 28.7 | 16.7% | **70.6%** |
| serial_acquirer | 102 | 28.0 | 7.8% | **75.0%** |
| energy | 94 | 27.6 | 2.1% | 50.0% |
| bank_financial | 0 | — | — | 68/68 rows correctly excluded via SIC |

(Flag/FP rates computed at the p80 threshold of 37.1; the table printed by
`scripts/analyze_backtest.py` uses the legacy >60 threshold, where only the
stress cohort registers — retained there to document the band-compression
finding.)

Key reads:

- **Stress cases score far above every control group (45.0 vs 28–32 — the gap
  widened from +7–8 points in July to +13–17) and are flagged three-quarters
  of the time, with the lowest FP rate among flagged (32.9%).** The screen
  concentrates on the right names more sharply than before.
- The price of that concentration: **FP rates among flagged controls rose**
  (57–80%, from 43–70%). The FP-heavy archetypes are unchanged — serial
  acquirers, capex-heavy AI infrastructure, and now staples (80%, but n=5
  flags). Archetype-aware anchors remain the identified fix and remain
  deferred (see "deliberately NOT changed").
- Hypergrowth SaaS flags still perform *better* than average (38.5% FP) —
  largely because 2021 flags preceded the 2022 growth-stock crash, a
  regime-specific outcome. This coexists with the wrong-signed Capital
  Integrity ICs vs fundamentals. Treat with caution.
- Energy now flags almost nothing (2.1%) and 8 of 102 rows withhold the
  overall entirely — a coverage artifact of the retirements on thin XBRL
  filers (DVN, OXY), not an all-clear; the report's §7 data-quality section
  exists for exactly this.

### Stress-case trajectories (qualitative)

- **PTON**: 72.9 at the 2021-12 as-of — the highest score in the regenerated
  backtest (July: 67.3 at 2021-09) — with a following-12M relative return of
  −56%; 70.4 at 2022-03 (−48%).
- **BYND**: 71.7 / 71.4 at the 2022-03 / 2022-06 as-ofs (−56% / −62%).
- **CVNA**: 66.1 at 2022-03 (−85%), ahead of the 2022 near-distress drawdown.
- **SMCI**: 58.7 at the 2024-06 as-of, directly before the August 2024 auditor
  resignation (−59%); the two 2024H2 as-ofs then show `skip_stale` because the
  10-K was genuinely late. **A stale-filing skip is itself a risk event** and
  is reported as such by the backtest tooling.
- **LCID**: 58.9 at 2023-03 (−97%).

Anecdotes on known cases — validating color, not statistical evidence — but
the ordering is notable: the five highest-scoring stress readings all precede
double-digit negative relative returns.

### Distress thermometer vs composite (kill-gate rerun)

`scripts/validate_thermometer.py` on the regenerated artifact, 6 stress
companies (98 quarters) vs 25 controls (412 quarters), all 31 regime caches
loaded and PIT-reconstructed:

| | Company-level AUC | Company-quarter AUC |
|---|---|---|
| Composite (`overall`) | 1.000 (July: 0.947) | 0.860 (July: 0.713) |
| Thermometer (2-cluster + regime) | 1.000 (July: 0.987) | 0.911 (July: 0.859) |

Company-level AUC saturates at n=6 stress companies and carries no
information at this size; the company-quarter figure is the one to read, and
there the thermometer's margin over the composite narrowed (from +0.15 to
+0.05) because the composite improved more under v0.4. The kill gate still
passes; it is still in-sample, ex-post-configured, and does not by itself
justify the retirement (see `docs/thermometer_season_ablation_2026Q2.md` for
the season half of the gate).

### Restatement prediction: not testable at this scale

8-K Item 4.02 events within 24 months of an as-of: **zero** in this
survivorship-biased universe (SMCI's 4.02 predates the window). No claim is
made; a delisting-inclusive universe is required.

## Changes applied in v0.3 (all provenance-commented in `scoring_config.py`)

1. **Block weights**: Earnings Quality 18→20, Cash Conversion 15→17, Capex
   Discipline 10→15, Balance Sheet 10→14; Revenue Quality 13→10, Working
   Capital 10→7, Capital Integrity 12→7, Narrative 12→10.
2. **Component exclusions** (weight → 0, still computed and reported):
   `buyback_offset_ratio` (wrong-signed; anchors punished non-buyback
   companies), `working_capital_swing_to_income` (wrong-signed everywhere).
3. **Within-block reweights**: `issuance_pressure` up (right-signed on all
   three outcomes); Working Capital redistributed to inventory spread + DIO
   trend.
4. **Direction bands**: positive < 32 / negative > 45 (empirical p50/p90 of
   the July distribution), replacing 35/60 which labeled almost nothing negative.
5. **Caveat text** updated to describe the partial calibration honestly; the
   uncalibrated-thresholds warning REMAINS on every output because anchors are
   still judgment-based and the backtest is directional evidence only.

## Changes between the July artifact and this regeneration (v0.4 code)

Landed in P0 (#2), the integration round (#7), rounds 14–15 (#10, #11) and
the watch/season PRs (#13, #14) — see git history for provenance:

- **Fourteen components retired** from the artifact: `beneish_tata`,
  `beneish_dsri`, `deferred_revenue_growth_spread`, `fcf_to_net_income`,
  `working_capital_swing_to_income`, `sbc_to_revenue`, `sbc_to_cfo`,
  `net_share_count_change`, `buyback_offset_ratio`,
  `incremental_revenue_per_capex`, `asset_quality_proxy`,
  `intangibles_to_assets`, `adjustment_recurrence_ratio`,
  `recurring_adjustment_terms`. One added: `risk_factor_expansion`.
- **TTM/YoY bases** and denominator/capex guards changed the surviving
  components' values on 44% of score cells (17,252 of 39,525 common cells).
- **Forward-outcome anchor fix** (the original reason for regeneration),
  isolated by comparing against the July artifact on identical score inputs:
  `op_margin_chg_4q` changed on 73 rows and became unavailable on 16;
  `fcf_margin_chg_4q` 112 / 16; `ni_growth_fwd_4q` 132 / 16. 115 of 1,275
  rows had a quarter that ended after the signal's filed quarter, i.e. were
  exposed to the old anchor mismatch. The effect on block ICs is second-order
  next to the scoring changes.
- **Return columns** differ only at the 1e-7 level from re-fetched adjusted
  closes.

## What was deliberately NOT changed

- Anchor values — the backtest ranks concern scores; it does not identify
  better breakpoints without overfitting this small sample.
- Direction bands — now mis-anchored (~p62/~p92) but changing them requires
  a reviewed snapshot diff; scheduled as its own change.
- Beneish M-score handling — n=342 rows had all components (coverage-
  limited); mixed signs; left as-is with its existing caveats.
- Sector anchor overrides — hypergrowth-SaaS-specific dilution anchors are the
  obvious candidate but would be fit to exactly the archetype that drove the
  wrong sign; deferred until a larger, less biased sample exists.
- Narrative Drift internals — untestable here.

## Reproducibility

- `tests/integration/test_calibration_reproducibility.py` pins config version,
  block weights, direction bands, component exclusions, and exact overall/
  block scores for the three committed real fixtures
  (`tests/golden_reports/calibration_snapshot.json`). Weight changes cannot
  land without a reviewed snapshot diff.
- The full backtest artifact is committed at
  `data/backtest/backtest_results.csv`; re-running `scripts/run_backtest.py`
  regenerates it (cache-dependent fetches; results stable for fixed as-ofs,
  given the XOM entity pin).

## Bottom line

Supported by evidence: the accrual family predicts forward margin
deterioration; capex-regime metrics are right-signed on fundamentals;
leverage and cash-generation metrics predicted returns in this rate cycle;
the highest scores in the sample sat on the right companies at the right
times (PTON, BYND, CVNA 2021–22; SMCI 2024); the stress cohort separates from
every control group more sharply than in July.

Weakened by this regeneration: the composite's top-quintile return separation
(now +4.7 pp hit-rate lift at p80 with a 50% FP rate, and non-monotone
quintile means). That is the same verdict the live 2026Q2 season reached
from the other direction, and it is why the composite is no longer shown.

Not supported: any claim of calibrated probabilities, restatement prediction,
ranking power below the top decile, or dilution as a forward-deterioration
signal in growth universes. The engine's outputs remain labeled accordingly.
