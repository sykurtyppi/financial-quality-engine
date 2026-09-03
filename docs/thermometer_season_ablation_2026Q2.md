# Thermometer vs composite — 2026Q2 season-archive ablation

Status: the second half of the P1-C kill gate (the first was the distressed-control
company-quarter AUC: thermometer 0.911 vs composite 0.860 on the 2026-09-03
regenerated backtest — 0.856 vs 0.713 on the July artifact; see
`scripts/validate_thermometer.py` and `docs/calibration_report.md`).
This asks the roadmap's other question: **would the live-season cards have read
differently — and more usefully — under the thermometer than the composite?**

Recomputed offline from cached companyfacts for the 13 names run during the
2026Q2 earnings-week season. Thermometer = 2-cluster AOM + regime dummies (the
kill-gate-passing config), anchor-based, no history/percentile framing.

| Ticker | Composite | Thermometer | Hottest cluster / regime |
|--------|-----------|-------------|--------------------------|
| AMPX   | 57.2 | **95.0** | Cash Generation & Funding / EBITDA<0, NI<0 ×2 |
| KTOS   | 50.6 | **77.4** | Cash Generation & Funding |
| CRM    | 26.5 | 60.8 | Balance Sheet & Leverage |
| BA     | 39.9 | 57.4 | Balance Sheet & Leverage / NI<0 |
| MXL    | 37.3 | 51.6 | Cash Generation & Funding |
| AMZN   | 35.4 | 41.6 | Cash Generation & Funding |
| AMKR   | 31.9 | 36.0 | Cash Generation & Funding |
| FPS    | (unscored / 10) | 35.9 | Balance Sheet & Leverage |
| GLW    | 29.1 | 32.1 | Balance Sheet & Leverage |
| NVDA   | 44.1 | 29.7 | Cash Generation & Funding |
| AAPL   | 20.9 | 29.4 | Balance Sheet & Leverage |
| META   | 27.5 | 28.2 | Cash Generation & Funding |
| MSFT   | 32.0 | 26.4 | Cash Generation & Funding |

## Findings

1. **Spread nearly doubles.** Composite 21–57 (the season doc reports the
   decision-relevant cluster as 31–58, "all Mixed"); thermometer 26–95. A wider,
   more separable range is a necessary condition for a triage headline.

2. **It separates the right names.** The two genuinely distressed names —
   **AMPX** (pre-profit, cash-burning; EBITDA<0 and losses two quarters running)
   and **KTOS** — go to the top. The composite buried AMPX at 57, indistinguishable
   from NVDA (44) and AMZN (47). The regime dummies do the work AMPX deserved.

3. **It de-emphasises the healthy hyperscalers.** MSFT 26, META 28, AAPL 29,
   NVDA 30 — correctly low. The composite had NVDA at 44 (over-flagged); the
   thermometer reads it 30.

4. **FPS** — the season's worst miss (composite scored it *lowest*, 10/100, while
   its sponsor sold four times) — reads 35.9 on the thermometer. Still not a
   headline number by itself; the offerings/capital-markets stream (P1-B) is what
   actually surfaces FPS, which is the point: **the thermometer is one readout, not
   the whole card.**

## Honest scope

This is a discrimination-spread and face-validity ablation, not an outcome-
validated one — the season names are too recent for clean outcome labels. The
outcome validation is the distressed-control AUC. Together they are the two
halves of the kill gate, and both now support leading the card with the
thermometer instead of the composite.

Regenerate: the per-name numbers come from `build_dataset` on cached
companyfacts + `compute_thermometer(block_scores, periods)`.
