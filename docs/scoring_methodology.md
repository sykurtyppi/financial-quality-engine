# Scoring Methodology

## Status: v0.3 — partially calibrated, read this first

Block weights, two component exclusions, and the direction bands were
adjusted from a 2021–2025 point-in-time walk-forward backtest
(**docs/calibration_report.md** — methodology, hit rates, false-positive
rates, and biases). Anchor points remain **judgment-based heuristics**, the
backtest universe is survivorship-biased, and the engine still attaches an
uncalibrated-thresholds caveat to every score. What the evidence supports:
the overall score as a top-quintile tail screen (hit rate 55.5% vs base 45.7%
for 12-month benchmark-relative underperformance, i.e. a 44.5% false-positive
rate); accrual metrics as forward margin-deterioration signals; capex-regime
metrics as the most consistently right-signed family. What it does not
support: calibrated probabilities, ranking power below the top quintile, or
SBC/dilution as a predictor in growth universes (kept as descriptive at
reduced weight).

## Score convention

All scores are **0–100 concern scores**: 0 = no concern, 100 = maximum
concern. This uniform orientation makes aggregation trivial and prevents the
classic mixed-direction scoring bug. Derived fields:

- **Direction** (v0.3, from the empirical score distribution — p50 ≈ 32,
  p90 ≈ 45, observed max ≈ 67): `< 32` positive · `32–45` mixed · `> 45`
  negative. The original 35/60 bands assumed the full 0–100 range is used;
  in practice scores compress to roughly 17–67.
- **Confidence** (per block): high if ≥ 70% of block weight computed with ≥ 3
  OK metrics; medium if ≥ 40% with ≥ 2; low otherwise.

## Metric → concern mapping

Each scored metric has a list of `(metric_value, concern_score)` anchor points.
The engine interpolates linearly between anchors and clamps outside the range.
Direction is encoded in the anchors (descending anchors = higher value is
better). Every component in the output exposes its value, its concern score,
its weight, AND its anchors — the mapping is fully reproducible by hand.

## Block scores

Weighted average of available (status `ok`) metric concern scores, with
weights renormalized over available weight. Renormalization is surfaced via
`data_coverage`; a block below 25% coverage scores `None` — the engine never
fabricates a midpoint for missing data.

## Blocks and v0 overall weights

| Block | Weight (v0.3) | v0 | Core question / calibration verdict |
|---|---|---|---|
| Earnings Quality | 20% | 18% | Are accruals inflating earnings? — margin-deterioration signal confirmed |
| Cash Conversion | 17% | 15% | Does cash confirm the income statement? — return signal confirmed |
| Capex Discipline | 15% | 10% | Is capex outrunning revenue? — right-signed on all outcomes |
| Balance Sheet Stress | 14% | 10% | Can the balance sheet absorb stress? — return signal (rate-cycle caveat) |
| Revenue Quality | 10% | 13% | Is revenue supported by receivables/deferred behavior? — near-zero measured signal |
| Narrative Drift | 10% | 12% | Is disclosure drifting defensively? — untestable in backtest, uncalibrated |
| Working Capital Stress | 7% | 10% | Is working capital masking softness? — weak/wrong-signed components |
| Capital Integrity | 7% | 12% | Is SBC/dilution eroding shareholders? — descriptive only; wrong-signed as predictor |

Two components are computed and reported but excluded from scoring (weight 0):
`buyback_offset_ratio` and `working_capital_swing_to_income` — both
wrong-signed in the backtest.

The Overall Quality Risk Score is the weighted average over blocks that scored;
below 50% available weight it is `None`.

## False-positive controls

1. **High-growth caveat**: when period revenue growth exceeds 40% (SGI > 1.4),
   growth-sensitive blocks (Revenue Quality, Working Capital, Capex, Capital
   Integrity) carry an explicit caveat that scale-up dynamics can mimic
   deterioration. The score is not suppressed — the interpretation is qualified.
2. **Financial institutions are excluded** entirely rather than mis-scored.
3. **Not-meaningful guards**: ratios on negative earnings bases are refused,
   not sign-flipped into nonsense.
4. **Quarterly Beneish caveat**: the M-score was estimated on annual data;
   quarterly use is flagged.

Known residual false-positive profiles that v0 does NOT yet normalize for
(handle at analyst review): serial acquirers (goodwill/intangibles), cyclical
inventory builders, early-stage infrastructure capex programs.

## Sector normalization hook

`SECTOR_ANCHOR_OVERRIDES[sector][metric_name]` replaces default anchors when
the company's sector matches. **Intentionally empty in v0** — populating it
without empirical distributions would be false precision. The calibration plan
(roadmap) derives per-sector percentile anchors from historical fundamentals.

## Calibration path (summary; see roadmap)

1. Build point-in-time historical fundamentals (delisting-inclusive).
2. Compute all metrics across the historical universe.
3. Replace hand-set anchors with per-sector percentile anchors.
4. Backtest block scores against forward outcomes (restatements, severe
   underperformance) and re-weight; publish the calibration report alongside
   the config.
