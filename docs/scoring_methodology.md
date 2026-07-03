# Scoring Methodology

## Status: v0 heuristic — read this first

Every weight, threshold, and anchor point in `app/config/scoring_config.py` is
a **judgment-based starting point**. Nothing is backtested, nothing is
sector-calibrated. The engine attaches this caveat to every score it emits.
Do not remove that caveat until the calibration work in `docs/roadmap.md` is
done and reviewed.

## Score convention

All scores are **0–100 concern scores**: 0 = no concern, 100 = maximum
concern. This uniform orientation makes aggregation trivial and prevents the
classic mixed-direction scoring bug. Derived fields:

- **Direction**: `< 35` positive · `35–60` mixed · `> 60` negative
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

| Block | Weight | Core question |
|---|---|---|
| Earnings Quality | 18% | Are accruals inflating reported earnings? |
| Cash Conversion | 15% | Does cash confirm the income statement? |
| Revenue Quality | 13% | Is revenue supported by receivables/deferred behavior? |
| Capital Integrity | 12% | Is SBC/dilution eroding shareholders quietly? |
| Narrative Drift | 12% | Is disclosure/language drifting defensively? |
| Working Capital Stress | 10% | Is working capital masking demand softness? |
| Capex Discipline | 10% | Is capex outrunning the revenue it should create? |
| Balance Sheet Stress | 10% | Can the balance sheet absorb stress? |

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
