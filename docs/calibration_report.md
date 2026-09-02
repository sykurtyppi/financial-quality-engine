# Calibration Report (v0.3)

Date: 2026-07-03 · Config version: 0.3.0 · Artifact: `data/backtest/backtest_results.csv`
Harness: `scripts/run_backtest.py` → `scripts/analyze_backtest.py`

> **Known artifact defect (2026-08-21):** `backtest_results.csv` was generated
> pre-F1-fix — the forward-outcome columns (`op_margin_chg_4q`,
> `fcf_margin_chg_4q`, `ni_growth_fwd_4q`) anchored on the last *ended* quarter
> while scores anchored on the last *filed* quarter, and the 4-quarter lookahead
> was positional with no contiguity guard. The code is fixed
> (`app/services/backtesting/outcomes.py`); regeneration and a refresh of every
> number in this report are scheduled after the 2026-08-26 NVDA print.

## Read this first

This is a **signal validity audit on free data**, not a statistically
validated calibration. It is sufficient to (a) demote components that are
demonstrably wrong-signed, (b) promote blocks with consistent right-signed
evidence, and (c) fix the score bands to the real distribution. It is NOT
sufficient to claim predictive power for the overall score, and this report
does not claim it.

## Methodology

- **Universe**: 75 companies, stratified: hypergrowth SaaS, capex-heavy AI/
  infrastructure, cyclicals, serial acquirers, energy, banks (exclusion-path
  check), staples/healthcare/retail/tech controls, and 7 known ex-post stress
  cases (SMCI, PTON, CVNA, OPEN, BYND, LCID, W) as a qualitative validation set.
- **Walk-forward**: as-of dates = calendar quarter ends 2021Q1–2025Q1 + 75-day
  filing lag → 1,275 company-quarter rows; 1,146 scored (`ok`), 119 excluded
  financials, 10 skips (stale/short history).
- **Point-in-time**: signals computed ONLY from facts with `filed` ≤ as-of
  (companyfacts filing dates). Facts without a filed date are dropped in PIT
  mode. Outcomes may see the future; signals cannot.
- **Outcomes**: 3/6/12-month SPY-relative returns (dividend-adjusted closes);
  realized forward operating-margin change (t+4 vs t); forward-vs-trailing
  4-quarter FCF-margin change; 8-K Item 4.02 (non-reliance) within 24 months.
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

### Overall score

Empirical distribution (n=1,141 with returns): min 17, p50 31.7, p90 45.1,
max 67.3 — the score uses roughly a third of its nominal 0–100 range.

Quintiles vs forward 12M SPY-relative return:

| Quintile (score range) | Mean rel. 12M | Median rel. 12M | n |
|---|---|---|---|
| Q1 (17–26) | +4.6% | −3.0% | 228 |
| Q2 (26–30) | +4.6% | −6.5% | 228 |
| Q3 (30–34) | +9.5% | −4.3% | 228 |
| Q4 (34–40) | +10.2% | −6.5% | 228 |
| Q5 (40–67) | **−5.3%** | **−14.5%** | 229 |

Interpretation: separation exists only in the top quintile; Q1–Q4 are not
monotone. The overall score behaves as a *tail screen*, not a ranking.

At the empirical top-quintile threshold (score ≥ 40.3):
- **Hit rate** (12M rel. return < −10%): **55.5%** vs base rate 45.7% (+9.8 pp)
- **False-positive rate among flagged: 44.5%** — nearly half of flags see
  benign forward returns. Any product use must present flags as review
  prompts, not predictions.
- Margin-deterioration hit rate at the same threshold: 28.7% vs base 26.3% —
  the *overall* score adds ~nothing for margins (the Earnings Quality *block*
  does; see below).

The original >60 "negative" band fired **once** in 1,146 scored rows (that
one: Peloton, Sept 2021 — see stress cases). Direction bands were therefore
re-anchored to the empirical distribution: positive < 32 (p50), negative > 45
(p90).

### Block-level signal (Spearman IC of concern vs outcome; negative = works)

| Block | vs 12M rel. return | vs fwd op-margin chg | vs fwd FCF-margin chg | Verdict |
|---|---|---|---|---|
| Earnings Quality | −0.02 | **−0.20** | −0.04 | Margin-deterioration signal confirmed (classic accrual result) |
| Cash Conversion | **−0.11** | +0.02 | +0.08* | Return signal confirmed; *FCF wrong-sign is mean reversion (bad FCF now → base-effect improvement) |
| Capex Discipline | **−0.11** | **−0.09** | **−0.15** | Right-signed everywhere — best all-around block |
| Balance Sheet Stress | **−0.07** | +0.03 | +0.05 | Return signal via leverage components (rate-cycle caveat) |
| Revenue Quality | −0.03 | +0.04 | +0.05 | Near-zero measured signal |
| Working Capital Stress | +0.01 | +0.07 | +0.04 | No signal as constructed |
| Capital Integrity | −0.01 | **+0.31 (wrong sign)** | **+0.25 (wrong sign)** | Broken as a *predictor* in this universe |
| Narrative Drift | — | — | — | Untestable (no documents) |

Strongest right-signed components vs 12M returns: net_debt_to_ebitda (−0.20),
interest_coverage (−0.18), fcf_margin (−0.17), fcf_to_net_income (−0.13),
issuance_pressure (−0.13), incremental_revenue_per_capex (−0.12).
Strongest predictors of forward margin deterioration: total_accruals / TATA
(−0.24/−0.25), capex_to_da (−0.17), accrual_trend (−0.12).

**Why Capital Integrity is wrong-signed**: in 2021–2025, high SBC/dilution
concern concentrated in hypergrowth software that subsequently *improved*
margins (scale leverage + the 2023–24 efficiency wave). SBC burden is a real
shareholder cost, but in this universe it behaved as a growth-stage marker,
not a deterioration predictor. The block is retained as *descriptive*
information at reduced weight.

### False-positive diagnostics by archetype (score ≥ p80 = 40.3)

| Archetype | Scored rows | Mean score | Flag rate | FP rate among flagged |
|---|---|---|---|---|
| stress_case | 95 | 40.1 | 44.2% | **28.6%** |
| hypergrowth_saas | 164 | 35.9 | 22.0% | 38.9% |
| control_retail | 119 | 33.7 | 25.2% | 43.3% |
| control_tech | 136 | 33.4 | 18.4% | 48.0% |
| control_staples | 85 | 32.9 | 16.5% | 42.9% |
| cyclical | 151 | 32.8 | 20.5% | 48.4% |
| control_healthcare | 85 | 32.6 | 18.8% | **68.8%** |
| capex_ai_infra | 102 | 31.3 | 12.7% | **61.5%** |
| energy | 102 | 30.6 | 11.8% | 33.3% |
| serial_acquirer | 102 | 30.1 | 9.8% | **70.0%** |
| bank_financial | 0 | — | — | 68/68 rows correctly excluded via SIC |

(Flag/FP rates computed at the p80 threshold of 40.3; the table printed by
`scripts/analyze_backtest.py` uses the legacy >60 threshold where flag rates
are ~0 — retained there to document the band-compression finding.)

Key reads:

- **Stress cases score highest as a group (+7–8 points vs controls) and have
  the lowest FP rate among flagged (28.6%)** — the screen concentrates on the
  right names.
- The FP-heavy archetypes are **serial acquirers (70%, n=10 flags)**,
  **healthcare (69%)**, and **capex-heavy AI infrastructure (62%)** — flags
  driven by goodwill/intangibles shares and capex-regime metrics on companies
  whose subsequent returns were fine. These are exactly the "model artifact /
  industry-normal" false-positive profiles v0.1 anticipated; archetype-aware
  anchors are the identified fix and are deferred (see "deliberately NOT
  changed").
- Hypergrowth SaaS flags performed *better* than average (38.9% FP) — but
  largely because 2021 flags preceded the 2022 growth-stock crash, a
  regime-specific outcome. This coexists with the wrong-signed SBC ICs vs
  fundamentals: the SaaS flags were "right" about returns for possibly the
  wrong reasons. Treat with caution.
- Energy scores are LOW mostly because of missing data (63–75% field
  coverage) — a suppressed score from absent data is a coverage artifact, not
  an all-clear; the report's §7 data-quality section exists for exactly this.

### Stress-case trajectories (qualitative, n=3 highlighted)

- **PTON**: 67.3 at 2021-09 as-of — the single highest score in the entire
  backtest, immediately preceding its collapse (following-12M relative return
  deeply negative). Scores stayed 40–60 through the 2022 drawdown.
- **SMCI**: 54.9 and 59.6 at the 2024-03/2024-06 as-ofs — its two highest
  readings — directly before the August 2024 auditor resignation; the two
  2024H2 as-ofs then show `skip_stale` because the 10-K was genuinely late.
  **Finding: a stale-filing skip is itself a risk event** and is now reported
  as such by the backtest tooling.
- **CVNA**: 55.0 at 2022-03, ahead of the 2022 near-distress drawdown.

Three anecdotes, selected because the companies are known cases — validating
color, not statistical evidence.

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
4. **Direction bands**: positive < 32 / negative > 45 (empirical p50/p90),
   replacing 35/60 which labeled almost nothing negative.
5. **Caveat text** updated to describe the partial calibration honestly; the
   uncalibrated-thresholds warning REMAINS on every output because anchors are
   still judgment-based and the backtest is directional evidence only.

## What was deliberately NOT changed

- Anchor values (except none) — the backtest ranks concern scores; it does not
  identify better breakpoints without overfitting this small sample.
- Beneish M-score handling — only n=357 rows had all 8 components (coverage-
  limited); mixed signs; left as-is with its existing caveats.
- Sector anchor overrides — hypergrowth-SaaS-specific SBC anchors are the
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
  regenerates it (cache-dependent fetches; results stable for fixed as-ofs).

## Bottom line

Supported by evidence: the overall score works as a *top-quintile tail
screen* (+10 pp hit-rate lift, 44% FP rate); the accrual family predicts
forward margin deterioration; capex-regime metrics are the most consistently
right-signed family; leverage metrics predicted returns in this rate cycle;
the highest scores in the sample sat on the right companies at the right
times (PTON 2021, SMCI 2024, CVNA 2022).

Not supported: any claim of calibrated probabilities, restatement prediction,
ranking power below the top quintile, or SBC/dilution as a forward-
deterioration signal in growth universes. The engine's outputs remain
labeled accordingly.
