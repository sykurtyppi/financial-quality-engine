# Earnings Quality & Narrative Drift Report — STRETCHCO

*Generated 2026-01-01 · Periods analyzed: FY2024Q1, FY2024Q2, FY2024Q3, FY2024Q4, FY2025Q1, FY2025Q2, FY2025Q3, FY2025Q4*

## 1. Executive Summary

No single composite grade is asserted — the 0–100 composite measured non-discriminating on the live season and is retired from the headline. This report is evidence detail, dimension by dimension. An EXPERIMENTAL distress reading (not yet validated) appears on the decision card; it is not a replacement aggregate.

The screen flagged 7 elevated-concern item(s) and 2 supportive item(s) for STRETCHCO in FY2025Q4. Key changes versus prior periods are listed in §5.

> Caveat: Scores use 0.4.0 config: v0.3 block weights from a small, survivorship-biased 2021-2025 backtest (~70 companies, point-in-time fundamentals), with the 2026-08 P0 corrections (TTM/YoY bases; measured-noise and wrong-signed signals retired to evidence). Anchor thresholds remain judgment-based heuristics and are not sector-normalized. Directional evidence only — treat as a screening aid, not a calibrated probability. Methodology and limits: docs/calibration_report.md.

## 2. Scorecard

All scores are 0–100 concern scores: 0 = no concern, 100 = maximum concern.

| Block | Score | Direction | Confidence | Coverage | Weight |
|---|---|---|---|---|---|
| Earnings Quality | 70 | Negative | high | 100% | 20% |
| Revenue Quality | 90 | Negative | medium | 100% | 10% |
| Cash Conversion | 81 | Negative | high | 100% | 17% |
| Working Capital Stress | 45 | Negative | medium | 100% | 7% |
| Capital Integrity | 63 | Negative | medium | 100% | 7% |
| Capex Discipline | 61 | Negative | high | 100% | 15% |
| Balance Sheet Stress | 28 | Positive | high | 100% | 14% |
| Narrative Drift | 40 | Mixed | medium | 75% | 10% |

> Earnings Quality: Beneish M-score note: Computed on TTM flows compared year-over-year; cutoffs are the annual-model cutoffs. TTM basis: flows summed over the 4 quarters ending this period.

## 3. Top Red Flags

- **Elevated concern: dso_trend** (FY2025Q4): dso_trend = 260 (formula: latest - mean(same fiscal quarter, prior years); period FY2025Q4; concern 90/100). Requires analyst review.
- **Receivables outpacing revenue** (FY2025Q4): receivables_growth_spread = 2.51 (formula: receivables growth - revenue growth; period FY2025Q4; concern 90/100). Requires analyst review.
- **Operating cash flow lagging reported earnings** (TTM FY2025Q4): cfo_to_net_income = 0.271 (formula: CFO / Net Income; period TTM FY2025Q4; concern 88/100). Requires analyst review.
- **Beneish screen in the elevated-attention zone** (TTM FY2025Q4): beneish_m_score = 0.0191 (formula: -4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI; period TTM FY2025Q4; concern 85/100). Requires analyst review.
- **Elevated concern: issuance_pressure** (FY2025Q4): issuance_pressure = 4.18 (formula: Issuance Proceeds / CFO; period FY2025Q4; concern 85/100). Requires analyst review.
- **Elevated concern: fcf_margin_trend** (TTM FY2025Q4): fcf_margin_trend = -0.0625 (formula: latest - mean(prior); period TTM FY2025Q4; concern 76/100). Requires analyst review.
- **Weak free-cash-flow generation** (TTM FY2025Q4): fcf_margin = -0.0681 (formula: (CFO - Capex) / Revenue; period TTM FY2025Q4; concern 74/100). Requires analyst review.

> Funding context (check before treating a weak free-cash-flow reading as deterioration): government grants, tax credits, customer advances, and milestone receipts may affect reported operating cash flow, investing cash flow, or net capital spending depending on their terms and presentation. Their classification is not separately modeled here — verify in the filing's liquidity/commitments notes (measured misread: AMKR 2026Q2).

## 4. Top Green Flags

- **Supportive: disclosure_volume_change** (FY2025Q4): disclosure_volume_change = 1.26 (formula: latest period word count / mean(prior period word counts); period FY2025Q4; concern 15/100). Supportive indicator.
- **Supportive: current_ratio** (FY2025Q4): current_ratio = 2.37 (formula: Current Assets / Current Liabilities; period FY2025Q4; concern 17/100). Supportive indicator.

## 5. What Changed This Period

- Total accruals: 0.055 (TTM FY2025Q3) -> 0.068 (TTM FY2025Q4)
- CFO / Net income: 0.42 (TTM FY2025Q3) -> 0.27 (TTM FY2025Q4)
- Receivables-vs-revenue growth spread: +251.3% (FY2025Q3) -> +251.3% (FY2025Q4)
- Days sales outstanding: 275 (FY2025Q3) -> 372 (FY2025Q4)
- SBC / Revenue: 12.0% (FY2025Q3) -> 13.0% (FY2025Q4)
- Capex / Revenue: 10.0% (FY2025Q3) -> 11.0% (FY2025Q4)
- FCF margin: -4.3% (TTM FY2025Q3) -> -6.8% (TTM FY2025Q4)

## 6. Narrative Drift Summary

Deterministic language analysis across the documented periods (QoQ, YoY, and trailing-8-quarter baselines where available). Findings are review prompts; each cites its evidence in §8.

- *adjustment_recurrence* (FY2025Q4): Adjustment term "restructuring" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …Results this quarter include restructuring charges related to our transformation program, which we believe are o…
  - Evidence: [FY2025Q2] …Results this quarter include restructuring charges related to our transformation program, which we believe are o…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "transformation" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …Results this quarter include restructuring charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA exc…
  - Evidence: [FY2025Q2] …Results this quarter include restructuring charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA exc…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "optimization" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …ntion was 108 percent and remaining performance obligations grew. Our optimization initiatives continue.…
  - Evidence: [FY2025Q2] …ntion was 108 percent and remaining performance obligations grew. Our optimization initiatives continue.…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "impairment" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …ime in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent and remaining performa…
  - Evidence: [FY2025Q2] …ime in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent and remaining performa…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "one-time" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …g charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and imp…
  - Evidence: [FY2025Q2] …g charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and imp…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "non-recurring" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …ich we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent a…
  - Evidence: [FY2025Q2] …ich we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent a…
- *adjustment_recurrence* (FY2025Q4): Adjustment term "adjusted ebitda" appears in 4 of 4 analyzed periods; repeated presentation of items as non-recurring warrants review.
  - Evidence: [FY2025Q1] …our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenu…
  - Evidence: [FY2025Q2] …our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenu…
- *kpi_removed* (FY2025Q4): KPI "Net revenue retention" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review.
- *kpi_removed* (FY2025Q4): KPI "RPO" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review.

## 7. Metric/Narrative Mismatches

Places where management narrative and deterministic metrics point in different directions. A mismatch is a question to resolve, not a conclusion — the narrative may be fully justified.

- **demand_narrative_vs_working_capital** (FY2025Q4, confidence high): Management emphasizes demand strength while receivables_growth_spread, dso_trend indicate working-capital deterioration. The demand narrative and the receivables/inventory build require joint review.
  - Metrics: receivables_growth_spread=2.51, dso_trend=260
  - Narrative evidence: NE-010 (§8b)

## 8. Evidence Ledger

### 8a. Metric evidence

| Metric | Period | Value | Formula | Inputs |
|---|---|---|---|---|
| cfo_to_net_income | TTM FY2025Q4 | 0.2713 | CFO / Net Income | cfo=121, net_income=446 |
| fcf_to_net_income | TTM FY2025Q4 | -0.6812 | (CFO - Capex) / Net Income | cfo=121, capex=425, net_income=446 |
| fcf_margin | TTM FY2025Q4 | -0.06812 | (CFO - Capex) / Revenue | cfo=121, capex=425, revenue=4.46e+03 |
| net_debt_to_ebitda | TTM FY2025Q4 | 1.179 | (Total Debt - Cash) / (EBIT + D&A) | total_debt=1.08e+03, cash_and_equivalents=260, ebit=535, depreciation_amortization=160 |
| total_accruals | TTM FY2025Q4 | 0.06844 | (Net Income - CFO) / Average Total Assets | net_income=446, cfo=121, total_assets=5.05e+03, total_assets_prior=4.45e+03 |
| beneish_dsri | TTM FY2025Q4 | 3.322 | (Receivables_t / Revenue_t) / (Receivables_t-1 / Revenue_t-1) | receivables=4.69e+03, revenue=4.46e+03, receivables_prior=1.31e+03, revenue_prior=4.12e+03 |
| beneish_gmi | TTM FY2025Q4 | 1 | GrossMargin_t-1 / GrossMargin_t | revenue=4.46e+03, cost_of_revenue=2.59e+03, revenue_prior=4.12e+03, cost_of_revenue_prior=2.39e+03 |
| beneish_aqi | TTM FY2025Q4 | 0.933 | [1 - (CA_t + PPE_t)/TA_t] / [1 - (CA_t-1 + PPE_t-1)/TA_t-1] | current_assets=1.99e+03, ppe_net=1.62e+03, total_assets=5.05e+03, current_assets_prior=1.71e+03, ppe_net_prior=1.38e+03, total_assets_prior=4.45e+03 |
| beneish_sgi | TTM FY2025Q4 | 1.082 | Revenue_t / Revenue_t-1 | revenue=4.46e+03, revenue_prior=4.12e+03 |
| beneish_depi | TTM FY2025Q4 | 1.156 | DeprRate_t-1 / DeprRate_t, DeprRate = D&A / (D&A + PP&E) | depreciation_amortization=160, ppe_net=1.62e+03, depreciation_amortization_prior=160, ppe_net_prior=1.38e+03 |
| beneish_sgai | TTM FY2025Q4 | 1 | (SGA_t / Revenue_t) / (SGA_t-1 / Revenue_t-1) | sga_expense=1.07e+03, revenue=4.46e+03, sga_expense_prior=989, revenue_prior=4.12e+03 |
| beneish_tata | TTM FY2025Q4 | 0.06438 | (Net Income - CFO) / Total Assets | net_income=446, cfo=121, total_assets=5.05e+03 |
| beneish_lvgi | TTM FY2025Q4 | 1.007 | [(Debt_t + CL_t)/TA_t] / [(Debt_t-1 + CL_t-1)/TA_t-1] | total_debt=1.08e+03, current_liabilities=840, total_assets=5.05e+03, total_debt_prior=920, current_liabilities_prior=760, total_assets_prior=4.45e+03 |
| beneish_m_score | TTM FY2025Q4 | 0.01909 | -4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI | dsri=3.32, gmi=1, aqi=0.933, sgi=1.08, depi=1.16, sgai=1, tata=0.0644, lvgi=1.01 |
| receivables_growth_spread | FY2025Q4 | 2.513 | receivables growth - revenue growth | receivables=4.69e+03, receivables_prior=1.31e+03, revenue=1.15e+03, revenue_prior=1.06e+03 |
| inventory_growth_spread | FY2025Q4 | 0.1882 | inventory growth - revenue growth | inventory=540, inventory_prior=425, revenue=1.15e+03, revenue_prior=1.06e+03 |
| deferred_revenue_growth_spread | FY2025Q4 | -0.1427 | deferred_revenue growth - revenue growth | deferred_revenue=272, deferred_revenue_prior=290, revenue=1.15e+03, revenue_prior=1.06e+03 |
| capex_growth_spread | FY2025Q4 | 0.6185 | Capex growth - Revenue growth | capex=126, capex_prior=74.3, revenue=1.15e+03, revenue_prior=1.06e+03 |
| dso | FY2025Q4 | 371.8 | (Receivables / Revenue) * days-in-period | receivables=4.69e+03, revenue=1.15e+03 |
| dio | FY2025Q4 | 73.71 | (Inventory / COGS) * days-in-period | inventory=540, cost_of_revenue=666 |
| dpo | FY2025Q4 | 45.5 | (Accounts Payable / COGS) * days-in-period | accounts_payable=333, cost_of_revenue=666 |
| working_capital_swing_to_income | FY2025Q4 | 11.39 | |Δ(Receivables + Inventory - Payables)| / |Net Income| | receivables=4.69e+03, inventory=540, accounts_payable=333, receivables_prior=3.41e+03, inventory_prior=509, accounts_payable_prior=327, net_income=115 |
| interest_coverage | FY2025Q4 | 7.255 | EBIT / Interest Expense | ebit=138, interest_expense=19 |
| debt_to_assets | FY2025Q4 | 0.2139 | Total Debt / Total Assets | total_debt=1.08e+03, total_assets=5.05e+03 |
| current_ratio | FY2025Q4 | 2.369 | Current Assets / Current Liabilities | current_assets=1.99e+03, current_liabilities=840 |
| asset_quality_proxy | FY2025Q4 | 0.2851 | 1 - (Current Assets + PP&E) / Total Assets | current_assets=1.99e+03, ppe_net=1.62e+03, total_assets=5.05e+03 |
| intangibles_to_assets | FY2025Q4 | 0.2337 | (Intangibles + Goodwill) / Total Assets | intangible_assets=370, goodwill=810, total_assets=5.05e+03 |
| goodwill_growth | FY2025Q4 | 0.03846 | (Goodwill_t - Goodwill_t-1) / Goodwill_t-1 | goodwill=810, goodwill_prior=780 |
| leverage_change | FY2025Q4 | 0.001616 | Debt_t/Assets_t - Debt_t-1/Assets_t-1 | total_debt=1.08e+03, total_assets=5.05e+03, total_debt_prior=1.04e+03, total_assets_prior=4.9e+03 |
| sbc_to_revenue | FY2025Q4 | 0.13 | SBC / Revenue | stock_based_compensation=149, revenue=1.15e+03 |
| sbc_to_cfo | FY2025Q4 | 26 | SBC / CFO | stock_based_compensation=149, cfo=5.74 |
| diluted_share_growth | FY2025Q4 | 0.01 | (DilutedShares_t - DilutedShares_t-1) / DilutedShares_t-1 | shares_diluted=107, shares_diluted_prior=106 |
| net_share_count_change | FY2025Q4 | 0.01 | (SharesOut_t - SharesOut_t-1) / SharesOut_t-1 | shares_outstanding=106, shares_outstanding_prior=105 |
| buyback_offset_ratio | FY2025Q4 | 0.5 | Buybacks / SBC | buybacks=74.7, stock_based_compensation=149 |
| issuance_pressure | FY2025Q4 | 4.179 | Issuance Proceeds / CFO | share_issuance_proceeds=24, cfo=5.74 |
| capex_to_revenue | FY2025Q4 | 0.11 | Capex / Revenue | capex=126, revenue=1.15e+03 |
| capex_to_da | FY2025Q4 | 3.159 | Capex / D&A | capex=126, depreciation_amortization=40 |
| accrual_trend | TTM FY2025Q4 | 0.02725 | latest total_accruals - mean(prior total_accruals) | latest=0.0684, prior_mean=0.0412, n_prior=3 |
| dso_trend | FY2025Q4 | 259.9 | latest - mean(same fiscal quarter, prior years) | latest=372, same_quarter_prior_mean=112, n_prior_years=1 |
| dio_trend | FY2025Q4 | 10.92 | latest - mean(same fiscal quarter, prior years) | latest=73.7, same_quarter_prior_mean=62.8, n_prior_years=1 |
| fcf_margin_trend | TTM FY2025Q4 | -0.0625 | latest - mean(prior) | latest=-0.0681, prior_mean=-0.00562 |
| capex_intensity_regime_shift | FY2025Q4 | 0.04 | mean(capex/revenue, last 4) - mean(capex/revenue, prior) | recent_mean=0.095, prior_mean=0.055 |
| incremental_revenue_per_capex | FY2025Q4 | 0.2059 | (Rev_t - Rev_t-4) / sum(Capex over last 4 periods) | revenue_end=1.15e+03, revenue_start=1.06e+03, total_capex=425 |
| adjustment_recurrence_ratio | FY2025Q4 | 1 | periods with adjustment language / periods analyzed | periods_analyzed=4 |
| recurring_adjustment_terms | FY2025Q4 | 7 | count of terms appearing in >= 3 periods | restructuring=4, transformation=4, optimization=4, impairment=4, one-time=4, non-recurring=4, adjusted ebitda=4 |
| kpi_removals | FY2025Q4 | 2 | KPIs in prior periods absent from latest |  |
| disclosure_volume_change | FY2025Q4 | 1.262 | latest period word count / mean(prior period word counts) | latest_words=53, prior_mean_words=42 |
| defensive_tone_change | FY2025Q4 | 0 | defensive-term density per 1k words vs trailing baseline | current_density=0, baseline_density=0 |

### 8b. Narrative evidence

| ID | Detector | Period | Basis | Confidence | Linked metrics | Excerpt |
|---|---|---|---|---|---|---|
| NE-001 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …Results this quarter include restructuring charges related to our transformation program, which we believe are o… \| [FY2025Q2] …Results this quarter include restructuring charges related to our transformation program, which we believe are o… |
| NE-002 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …Results this quarter include restructuring charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA exc… \| [FY2025Q2] …Results this quarter include restructuring charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA exc… |
| NE-003 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …ntion was 108 percent and remaining performance obligations grew. Our optimization initiatives continue.… \| [FY2025Q2] …ntion was 108 percent and remaining performance obligations grew. Our optimization initiatives continue.… |
| NE-004 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …ime in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent and remaining performa… \| [FY2025Q2] …ime in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent and remaining performa… |
| NE-005 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …g charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and imp… \| [FY2025Q2] …g charges related to our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and imp… |
| NE-006 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …ich we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent a… \| [FY2025Q2] …ich we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenue retention was 108 percent a… |
| NE-007 | adjustment_recurrence | FY2025Q4 | trailing8 | medium | — | [FY2025Q1] …our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenu… \| [FY2025Q2] …our transformation program, which we believe are one-time in nature. Adjusted EBITDA excludes these non-recurring costs and impairment charges. Net revenu… |
| NE-008 | kpi_removed | FY2025Q4 | qoq | medium | — | KPI "Net revenue retention" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review. |
| NE-009 | kpi_removed | FY2025Q4 | qoq | medium | — | KPI "RPO" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review. |
| NE-010 | mismatch:demand_narrative_vs_working_capital | FY2025Q4 | point | high | receivables_growth_spread, dso_trend | …Demand remains strong across our end markets and we saw strong momentum with enterprise customers. Results this… |

## 9. Metric Detail and Data Quality

How to read this section — four distinct situations, never conflated:

- **Computed** — the metric was calculated; whether it is a concern is judged in the scorecard (§2) and red flags (§3), not by its mere presence here.
- **Data unavailable** — the filer does not disclose an input (or our mapping could not locate it). This is a coverage gap, NOT evidence of a problem.
- **Not meaningful** — inputs exist but the ratio is undefined for this company's situation (e.g. earnings-based ratios during a loss period). The note says why; review the underlying levels directly.
- **Sector/model caveats** — quoted lines under the scorecard (§2); they qualify interpretation (e.g. high-growth profile) without changing computed values.

Coverage: **48 computed**, 0 not meaningful, 2 with data unavailable (out of 50 metrics).

### Computed metrics

| Metric | Period | Value | Note |
|---|---|---|---|
| cfo_to_net_income | TTM FY2025Q4 | 0.2713 | TTM basis: flows summed over the 4 quarters ending this period. |
| fcf_to_net_income | TTM FY2025Q4 | -0.6812 | TTM basis: flows summed over the 4 quarters ending this period. |
| fcf_margin | TTM FY2025Q4 | -0.06812 | TTM basis: flows summed over the 4 quarters ending this period. |
| net_debt_to_ebitda | TTM FY2025Q4 | 1.179 | TTM basis: flows summed over the 4 quarters ending this period. |
| total_accruals | TTM FY2025Q4 | 0.06844 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_dsri | TTM FY2025Q4 | 3.322 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_gmi | TTM FY2025Q4 | 1 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_aqi | TTM FY2025Q4 | 0.933 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_sgi | TTM FY2025Q4 | 1.082 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_depi | TTM FY2025Q4 | 1.156 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_sgai | TTM FY2025Q4 | 1 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_tata | TTM FY2025Q4 | 0.06438 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_lvgi | TTM FY2025Q4 | 1.007 | TTM basis: flows summed over the 4 quarters ending this period. |
| beneish_m_score | TTM FY2025Q4 | 0.01909 | Computed on TTM flows compared year-over-year; cutoffs are the annual-model cutoffs. TTM basis: flows summed over the 4 quarters ending this period. |
| receivables_growth_spread | FY2025Q4 | 2.513 | YoY basis: compared to the same fiscal quarter one year earlier. |
| inventory_growth_spread | FY2025Q4 | 0.1882 | YoY basis: compared to the same fiscal quarter one year earlier. |
| deferred_revenue_growth_spread | FY2025Q4 | -0.1427 | YoY basis: compared to the same fiscal quarter one year earlier. |
| capex_growth_spread | FY2025Q4 | 0.6185 | YoY basis: compared to the same fiscal quarter one year earlier. |
| dso | FY2025Q4 | 371.8 |  |
| dio | FY2025Q4 | 73.71 |  |
| dpo | FY2025Q4 | 45.5 |  |
| working_capital_swing_to_income | FY2025Q4 | 11.39 |  |
| interest_coverage | FY2025Q4 | 7.255 |  |
| debt_to_assets | FY2025Q4 | 0.2139 |  |
| current_ratio | FY2025Q4 | 2.369 |  |
| asset_quality_proxy | FY2025Q4 | 0.2851 |  |
| intangibles_to_assets | FY2025Q4 | 0.2337 |  |
| goodwill_growth | FY2025Q4 | 0.03846 |  |
| leverage_change | FY2025Q4 | 0.001616 |  |
| sbc_to_revenue | FY2025Q4 | 0.13 |  |
| sbc_to_cfo | FY2025Q4 | 26 |  |
| diluted_share_growth | FY2025Q4 | 0.01 |  |
| net_share_count_change | FY2025Q4 | 0.01 |  |
| buyback_offset_ratio | FY2025Q4 | 0.5 |  |
| issuance_pressure | FY2025Q4 | 4.179 |  |
| capex_to_revenue | FY2025Q4 | 0.11 |  |
| capex_to_da | FY2025Q4 | 3.159 |  |
| accrual_trend | TTM FY2025Q4 | 0.02725 |  |
| dso_trend | FY2025Q4 | 259.9 |  |
| dio_trend | FY2025Q4 | 10.92 |  |
| fcf_margin_trend | TTM FY2025Q4 | -0.0625 |  |
| capex_intensity_regime_shift | FY2025Q4 | 0.04 |  |
| incremental_revenue_per_capex | FY2025Q4 | 0.2059 | Capex-to-revenue conversion lags may exceed the window; low values prompt review, not verdicts. |
| adjustment_recurrence_ratio | FY2025Q4 | 1 |  |
| recurring_adjustment_terms | FY2025Q4 | 7 |  |
| kpi_removals | FY2025Q4 | 2 | Removed: Net revenue retention, RPO |
| disclosure_volume_change | FY2025Q4 | 1.262 |  |
| defensive_tone_change | FY2025Q4 | 0 |  |

### Data unavailable (reported, not silently dropped)

| Metric | Period | Missing inputs |
|---|---|---|
| guidance_shift | n/a | guidance discussion in >= 2 periods |
| risk_factor_expansion | n/a | risk-factor sections for >= 2 comparable periods |

## 10. Analyst Review Questions

- Management describes demand as strong; what explains the concurrent receivables/inventory build relative to revenue?
- What drove receivables growth ahead of revenue this period — payment-term changes, channel mix, or collection timing?
- Which accrual items explain the gap between net income and operating cash flow, and are they expected to reverse?
- Regarding FY2025Q4: KPI "Net revenue retention" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review.
- Regarding FY2025Q4: KPI "RPO" was discussed in prior periods (FY2025Q2, FY2025Q3) but is not mentioned in FY2025Q4. Reduced disclosure of a previously highlighted metric warrants review.

## 11. Disclaimer

This report is an automated, formula-driven screening analysis of publicly reported financial data. It expresses opinions about earnings quality and presentation risk derived from the disclosed formulas herein. It does not allege fraud, misconduct, or wrongdoing by any company or person, and it is not investment advice. Elevated scores identify areas that warrant analyst review; they are not conclusions. Data may contain errors; formulas use v0 heuristic thresholds that are not backtested or sector-normalized.
