# Review: `aapl_comparative_rounding` (DRAFT — `reviewed: null`)

**AAPL**, CIK 320193, as of 2026-05-01, restatement window from 2024-01-01.

**Why this case exists.** Fourth quarters derived as the 10-K fiscal year minus the 10-Q nine months (ytd_diff), and real comparatives re-filed at a different rounding (LongTermDebt 90,678m in the 10-K, 90,700m in later 10-Qs) on a tag the engine does not score: nothing scored moves, so the case must stay clean.

This draft was built offline from the committed trimmed fixture `tests/fixtures/real/companyfacts_AAPL_trimmed.json` (`scripts/make_corpus_case.py --from-facts`). Its `expected` block is a COPY of what the engine observed; nothing in it has been checked against a filing. The fixture carries no filing index, so the 8-K 4.02 check did not run (`non_reliance` is empty by construction, not by observation).

## What to check, and against which filing

1. `LongTermDebt` for 2025-09-27: 90,678m in the 10-K (`0000320193-25-000079`, filed 2025-10-31), 90,700m in the 10-Qs of 2026-01-30 and 2026-05-01. Confirm it is rounding in the 10-Q debt note, not a revision. The engine does not score this tag (`total_debt` is `LongTermDebtNoncurrent+LongTermDebtCurrent+CommercialPaper`), so it must produce no footprint either way.
2. FY2025Q4 `revenue` = 10-K fiscal-2025 revenue minus the nine months in the Q3 10-Q (`0000320193-25-000073`). Check the result against the quarter the 10-K implies (Apple's 10-K does not report Q4 alone; its Q4 press release 8-K does).
3. `total_debt` components: confirm Apple reports term debt non-current, term debt current and commercial paper under those three tags in each quarter of the window, and nothing else the engine should add.
4. `goodwill`, `share_issuance_proceeds` and `interest_expense` are named not inspectable. The last is selected (`InterestExpense`) but its latest fact ends 2023-09-30, so it fills 0 of the 8 quarters: confirm Apple stopped reporting interest expense as a separate line after fiscal 2023, and that it reports neither goodwill nor share-issuance proceeds in this window.

## What the engine observed

- Tier-1 events: none
- Restatement footprints (raw and derived): none
- Evidence coverage: 88% of scored fields inspected; not inspected: `goodwill` (no series mapped this run), `interest_expense` (selected series has no period on or after 2024-01-01), `share_issuance_proceeds` (no series mapped this run)
- Fields mapped: 25 (tags in `case.json` → `expected.selections`)

Mapper notes:

- `shares_outstanding`: Share counts matched from cover-page dates within 60 days after quarter end.
- `shares_diluted`: Weighted-average share counts are not additive; quarters without a directly reported value stay missing (no Q4 derivation).

Sample of mapped values and the facts behind them:

| Quarter | Ends | Field | Value | Method | Facts (+ added, − subtracted) |
|---|---|---|---|---|---|
| FY2024Q3 | 2024-06-29 | `revenue` | 85,777,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-03-31→2024-06-29 (10-Q `0000320193-25-000073`) |
| FY2024Q3 | 2024-06-29 | `interest_expense` | — | not mapped | |
| FY2024Q3 | 2024-06-29 | `shares_outstanding` | 15,204,137,000 | nearest | + `EntityCommonStockSharesOutstanding` →2024-07-19 (10-Q `0000320193-24-000081`) |
| FY2024Q3 | 2024-06-29 | `total_debt` | 101,304,000,000 | composite | + `LongTermDebtNoncurrent` →2024-06-29 (10-Q `0000320193-24-000081`)<br>+ `LongTermDebtCurrent` →2024-06-29 (10-Q `0000320193-24-000081`)<br>+ `CommercialPaper` →2024-06-29 (10-Q `0000320193-24-000081`) |
| FY2024Q4 | 2024-09-28 | `revenue` | 94,930,000,000 | ytd_diff | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2023-10-01→2024-09-28 (10-K `0000320193-25-000079`)<br>− `RevenueFromContractWithCustomerExcludingAssessedTax` 2023-10-01→2024-06-29 (10-Q `0000320193-25-000073`) |
| FY2024Q4 | 2024-09-28 | `interest_expense` | — | not mapped | |
| FY2024Q4 | 2024-09-28 | `shares_outstanding` | 15,115,823,000 | nearest | + `EntityCommonStockSharesOutstanding` →2024-10-18 (10-K `0000320193-24-000123`) |
| FY2024Q4 | 2024-09-28 | `total_debt` | 106,629,000,000 | composite | + `LongTermDebtNoncurrent` →2024-09-28 (10-K `0000320193-25-000079`)<br>+ `LongTermDebtCurrent` →2024-09-28 (10-K `0000320193-25-000079`)<br>+ `CommercialPaper` →2024-09-28 (10-K `0000320193-25-000079`) |
| FY2025Q1 | 2024-12-28 | `revenue` | 124,300,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-09-29→2024-12-28 (10-Q `0000320193-26-000006`) |
| FY2025Q1 | 2024-12-28 | `interest_expense` | — | not mapped | |
| FY2025Q1 | 2024-12-28 | `shares_outstanding` | 15,022,073,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-01-17 (10-Q `0000320193-25-000008`) |
| FY2025Q1 | 2024-12-28 | `total_debt` | 96,799,000,000 | composite | + `LongTermDebtNoncurrent` →2024-12-28 (10-Q `0000320193-25-000008`)<br>+ `LongTermDebtCurrent` →2024-12-28 (10-Q `0000320193-25-000008`)<br>+ `CommercialPaper` →2024-12-28 (10-Q `0000320193-25-000008`) |
| FY2025Q2 | 2025-03-29 | `revenue` | 95,359,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-12-29→2025-03-29 (10-Q `0000320193-26-000013`) |
| FY2025Q2 | 2025-03-29 | `interest_expense` | — | not mapped | |
| FY2025Q2 | 2025-03-29 | `shares_outstanding` | 14,935,826,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-04-18 (10-Q `0000320193-25-000057`) |
| FY2025Q2 | 2025-03-29 | `total_debt` | 98,186,000,000 | composite | + `LongTermDebtNoncurrent` →2025-03-29 (10-Q `0000320193-25-000057`)<br>+ `LongTermDebtCurrent` →2025-03-29 (10-Q `0000320193-25-000057`)<br>+ `CommercialPaper` →2025-03-29 (10-Q `0000320193-25-000057`) |
| FY2025Q3 | 2025-06-28 | `revenue` | 94,036,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-03-30→2025-06-28 (10-Q `0000320193-25-000073`) |
| FY2025Q3 | 2025-06-28 | `interest_expense` | — | not mapped | |
| FY2025Q3 | 2025-06-28 | `shares_outstanding` | 14,840,390,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-07-18 (10-Q `0000320193-25-000073`) |
| FY2025Q3 | 2025-06-28 | `total_debt` | 101,698,000,000 | composite | + `LongTermDebtNoncurrent` →2025-06-28 (10-Q `0000320193-25-000073`)<br>+ `LongTermDebtCurrent` →2025-06-28 (10-Q `0000320193-25-000073`)<br>+ `CommercialPaper` →2025-06-28 (10-Q `0000320193-25-000073`) |
| FY2025Q4 | 2025-09-27 | `revenue` | 102,466,000,000 | ytd_diff | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-09-29→2025-09-27 (10-K `0000320193-25-000079`)<br>− `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-09-29→2025-06-28 (10-Q `0000320193-25-000073`) |
| FY2025Q4 | 2025-09-27 | `interest_expense` | — | not mapped | |
| FY2025Q4 | 2025-09-27 | `shares_outstanding` | 14,776,353,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-10-17 (10-K `0000320193-25-000079`) |
| FY2025Q4 | 2025-09-27 | `total_debt` | 98,657,000,000 | composite | + `LongTermDebtNoncurrent` →2025-09-27 (10-Q `0000320193-26-000013`)<br>+ `LongTermDebtCurrent` →2025-09-27 (10-Q `0000320193-26-000013`)<br>+ `CommercialPaper` →2025-09-27 (10-Q `0000320193-26-000013`) |
| FY2026Q1 | 2025-12-27 | `revenue` | 143,756,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-09-28→2025-12-27 (10-Q `0000320193-26-000006`) |
| FY2026Q1 | 2025-12-27 | `interest_expense` | — | not mapped | |
| FY2026Q1 | 2025-12-27 | `shares_outstanding` | 14,681,140,000 | nearest | + `EntityCommonStockSharesOutstanding` →2026-01-16 (10-Q `0000320193-26-000006`) |
| FY2026Q1 | 2025-12-27 | `total_debt` | 90,509,000,000 | composite | + `LongTermDebtNoncurrent` →2025-12-27 (10-Q `0000320193-26-000006`)<br>+ `LongTermDebtCurrent` →2025-12-27 (10-Q `0000320193-26-000006`)<br>+ `CommercialPaper` →2025-12-27 (10-Q `0000320193-26-000006`) |
| FY2026Q2 | 2026-03-28 | `revenue` | 111,184,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-12-28→2026-03-28 (10-Q `0000320193-26-000013`) |
| FY2026Q2 | 2026-03-28 | `interest_expense` | — | not mapped | |
| FY2026Q2 | 2026-03-28 | `shares_outstanding` | 14,687,356,000 | nearest | + `EntityCommonStockSharesOutstanding` →2026-04-17 (10-Q `0000320193-26-000013`) |
| FY2026Q2 | 2026-03-28 | `total_debt` | 84,711,000,000 | composite | + `LongTermDebtNoncurrent` →2026-03-28 (10-Q `0000320193-26-000013`)<br>+ `LongTermDebtCurrent` →2026-03-28 (10-Q `0000320193-26-000013`)<br>+ `CommercialPaper` →2026-03-28 (10-Q `0000320193-26-000013`) |

## Filings behind these values

| Accession | Form | Filed |
|---|---|---|
| `0000320193-24-000081` | 10-Q | 2024-08-02 |
| `0000320193-24-000123` | 10-K | 2024-11-01 |
| `0000320193-25-000008` | 10-Q | 2025-01-31 |
| `0000320193-25-000057` | 10-Q | 2025-05-02 |
| `0000320193-25-000073` | 10-Q | 2025-08-01 |
| `0000320193-25-000079` | 10-K | 2025-10-31 |
| `0000320193-26-000006` | 10-Q | 2026-01-30 |
| `0000320193-26-000013` | 10-Q | 2026-05-01 |

## To pin it

1. Read each check above in the named filing (EDGAR full-text or the filing index for the accession).
2. Where the engine is wrong, correct `expected` in `case.json` from the filing — never to make the case pass.
3. Fill in `reviewed` (`by`, `on`, `filings_read` with the accessions read), then move the directory to `tests/corpus/`. The corpus gate then holds the engine to it, and `corpus-real` turns green.
