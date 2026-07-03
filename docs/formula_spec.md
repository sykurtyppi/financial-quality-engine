# Formula Specification (v1)

All monetary inputs are the filer's reporting currency, unscaled. Period-pair
formulas use the current period `t` and the immediately preceding period `t-1`
in the supplied series (quarter-over-quarter for quarterly input). Day-count
metrics use 91 days for quarters, 365 for fiscal years.

Statuses: a formula returns `missing_data` if any input is absent, and
`not_meaningful` (with a note) when arithmetic would be valid but economically
meaningless (documented per formula below).

## 1. Accruals / cash earnings reality

| Metric | Formula | Not meaningful when |
|---|---|---|
| `total_accruals` | (Net Income − CFO) / Average Total Assets | avg assets ≤ 0 |
| `cfo_to_net_income` | CFO / Net Income | NI ≤ 0 |
| `fcf_to_net_income` | (CFO − Capex) / Net Income | NI ≤ 0 |
| `fcf_margin` | (CFO − Capex) / Revenue | revenue ≤ 0 |
| `accrual_trend` | latest total_accruals − mean(prior) | < 3 OK observations → missing |

## 2. Beneish components (Beneish 1999)

| Metric | Formula |
|---|---|
| `beneish_dsri` | (Rec_t/Rev_t) / (Rec_t−1/Rev_t−1) |
| `beneish_gmi` | GM_t−1 / GM_t, GM = (Rev − COGS)/Rev |
| `beneish_aqi` | [1 − (CA_t + PPE_t)/TA_t] / [1 − (CA_t−1 + PPE_t−1)/TA_t−1] |
| `beneish_sgi` | Rev_t / Rev_t−1 |
| `beneish_depi` | DeprRate_t−1 / DeprRate_t, DeprRate = D&A/(D&A + PPE) |
| `beneish_sgai` | (SGA_t/Rev_t) / (SGA_t−1/Rev_t−1) |
| `beneish_lvgi` | [(Debt_t + CL_t)/TA_t] / [(Debt_t−1 + CL_t−1)/TA_t−1] |
| `beneish_tata` | (NI − CFO) / TA |
| `beneish_m_score` | −4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI |

Documented deviations:

- **LVGI** uses `total_debt + current_liabilities` rather than the canonical
  LTD + CL split (normalized data rarely splits debt reliably). When
  `total_debt` includes short-term debt also inside `current_liabilities`,
  short-term debt is double-counted; the ratio-of-ratios form dampens the bias.
- **TATA** uses the post-SFAS-95 cash-flow form (NI − CFO)/TA, not the original
  balance-sheet delta form.
- The model was **estimated on annual data**; quarterly application is a
  screening convenience and carries an explicit caveat in the output.
- The M-score requires **all 8 components**; a partial score is never emitted.

## 3. Working capital / revenue quality

| Metric | Formula | Not meaningful when |
|---|---|---|
| `receivables_growth_spread` | Rec growth − Rev growth | prior rev ≤ 0, prior rec = 0 |
| `inventory_growth_spread` | Inv growth − Rev growth | prior rev ≤ 0, prior inv = 0 |
| `deferred_revenue_growth_spread` | DefRev growth − Rev growth (concern when negative) | same pattern |
| `dso` | (Receivables / Revenue) × days | revenue ≤ 0 |
| `dio` | (Inventory / COGS) × days | COGS ≤ 0 |
| `dpo` | (Payables / COGS) × days | COGS ≤ 0 |
| `working_capital_swing_to_income` | |Δ(Rec + Inv − Pay)| / |NI| | NI = 0 |
| `dso_trend`, `dio_trend` | latest − mean(prior), ≥ 3 OK observations | |

## 4. Balance sheet stress

| Metric | Formula | Not meaningful when |
|---|---|---|
| `net_debt_to_ebitda` | (Debt − Cash) / (EBIT + D&A) | EBITDA ≤ 0 |
| `interest_coverage` | EBIT / Interest Expense | interest = 0 or negative input |
| `debt_to_assets` | Debt / Assets | assets ≤ 0 |
| `current_ratio` | CA / CL | CL = 0 |
| `asset_quality_proxy` | 1 − (CA + PPE)/TA | assets ≤ 0 |
| `intangibles_to_assets` | (Intangibles + Goodwill) / TA | assets ≤ 0 |
| `goodwill_growth` | ΔGoodwill / prior Goodwill | prior ≤ 0 |
| `leverage_change` | Debt_t/TA_t − Debt_t−1/TA_t−1 | assets ≤ 0 |

## 5. Capital structure / dilution

| Metric | Formula | Not meaningful when |
|---|---|---|
| `sbc_to_revenue` | SBC / Revenue | revenue ≤ 0 |
| `sbc_to_cfo` | SBC / CFO | CFO ≤ 0 |
| `diluted_share_growth` | ΔDiluted shares / prior | prior ≤ 0 |
| `net_share_count_change` | ΔShares outstanding / prior | prior ≤ 0 |
| `buyback_offset_ratio` | Buybacks / SBC | SBC = 0 |
| `issuance_pressure` | Issuance proceeds / CFO | CFO ≤ 0 |

## 6. Capex regime

| Metric | Formula | Notes |
|---|---|---|
| `capex_to_revenue` | Capex / Revenue | |
| `capex_growth_spread` | Capex growth − Rev growth | prior capex = 0 → not meaningful |
| `capex_to_da` | Capex / D&A | |
| `capex_intensity_regime_shift` | mean(capex/rev, last 4) − mean(capex/rev, prior) | needs ≥ 6 usable periods |
| `incremental_revenue_per_capex` | (Rev_t − Rev_t−4) / Σ Capex last 4 periods | conversion lag caveat attached |

## 7. Narrative metrics (deterministic text analytics)

| Metric | Definition |
|---|---|
| `adjustment_recurrence_ratio` | Fraction of documented periods containing ≥ 1 adjustment term (word-boundary matched from a fixed term list) |
| `recurring_adjustment_terms` | Count of distinct adjustment terms appearing in ≥ 3 periods |
| `kpi_removals` | KPIs (curated dictionary) present in the prior 2 documented periods but absent from the latest |
| `disclosure_volume_change` | Latest period word count / mean of prior periods (needs ≥ 3 documented periods) |

Term list and KPI dictionary: `app/services/narrative/adjustment_language.py`
and `kpi_drift.py`. KPI *definition-change* detection is deferred (roadmap) —
it requires semantic comparison and is not approximated deterministically.
