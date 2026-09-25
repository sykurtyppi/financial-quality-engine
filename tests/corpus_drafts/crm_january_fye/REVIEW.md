# Review: `crm_january_fye` (DRAFT — `reviewed: null`)

**CRM**, CIK 1108524, as of 2026-05-28, restatement window from 2024-02-01.

**Why this case exists.** A January fiscal year end (FY2027Q1 ends 2026-04-30): quarter labels, fourth quarters derived as the 10-K year minus the 10-Q nine months, a composed SG&A, and a proxy (DEF 14A) fact in the payload.

This draft was built offline from the committed trimmed fixture `tests/fixtures/real/companyfacts_CRM_trimmed.json` (`scripts/make_corpus_case.py --from-facts`). Its `expected` block is a COPY of what the engine observed; nothing in it has been checked against a filing. The fixture carries no filing index, so the 8-K 4.02 check did not run (`non_reliance` is empty by construction, not by observation).

## What to check, and against which filing

1. Fiscal calendar: FY2027Q1 ends 2026-04-30, FY2026Q4 ends 2026-01-31. Confirm against the 10-Q `0001108524-26-000127` (filed 2026-05-28) and the 10-K `0001108524-26-000060` (filed 2026-03-02) cover pages.
2. FY2026Q4 `interest_expense` is 122m, derived as the 10-K year minus the nine months in `0001108524-25-000238`; every other quarter is 67–68m. Check that the fourth quarter really carried ~122m (new debt in the quarter?) and that the 10-K's annual `InterestExpenseDebt` is the same concept as the 10-Q year-to-date figure. FY2027Q1 `interest_expense` is not mapped: check whether the Q1 FY2027 10-Q reports it under another tag.
3. FY2027Q1 `shares_outstanding` is 819m (cover dated 2026-05-21) against 923m a quarter earlier. Confirm the cover-page count; an 11% fall in one quarter is either a real event (a large accelerated repurchase) or a data error the engine should not score silently.
4. `sga_expense` is composed from separate sales-and-marketing and general-and-administrative tags; confirm CRM reports no combined SG&A line.
5. `accounts_payable`, `inventory` and `share_issuance_proceeds` are named not inspectable: the payload holds none of their candidate tags (for payables: `AccountsPayableCurrent`, `AccountsPayableAndAccruedLiabilitiesCurrent`, `AccountsPayableTradeCurrent`). Check the balance sheet for the tag CRM does use for payables; if it is a standard us-gaap payables concept, the engine's candidate list has a gap.
6. The payload holds one proxy fact: `NetIncomeLoss` from the DEF 14A `0001108524-26-000085` (filed 2026-04-16; the pay-versus-performance table). Confirm it equals the 10-K's annual net income, and that a proxy fact never replaces a 10-K or 10-Q figure in a scored value.

## What the engine observed

- Tier-1 events: none
- Restatement footprints (raw and derived): none
- Evidence coverage: 88% of scored fields inspected; not inspected: `accounts_payable` (no series mapped this run), `inventory` (no series mapped this run), `share_issuance_proceeds` (no series mapped this run)
- Fields mapped: 24 (tags in `case.json` → `expected.selections`)

Mapper notes:

- `shares_outstanding`: Share counts matched from cover-page dates within 60 days after quarter end.
- `sga_expense`: SG&A composed from separate S&M and G&A tags.
- `shares_diluted`: Weighted-average share counts are not additive; quarters without a directly reported value stay missing (no Q4 derivation).
- `total_debt`: Short-term borrowings unavailable or zero; not included.

Sample of mapped values and the facts behind them:

| Quarter | Ends | Field | Value | Method | Facts (+ added, − subtracted) |
|---|---|---|---|---|---|
| FY2025Q2 | 2024-07-31 | `revenue` | 9,325,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-05-01→2024-07-31 (10-Q `0001108524-25-000088`) |
| FY2025Q2 | 2024-07-31 | `interest_expense` | 68,000,000 | direct | + `InterestExpenseDebt` 2024-05-01→2024-07-31 (10-Q `0001108524-25-000088`) |
| FY2025Q2 | 2024-07-31 | `shares_outstanding` | 956,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2024-08-23 (10-Q `0001108524-24-000022`) |
| FY2025Q2 | 2024-07-31 | `total_debt` | 8,430,000,000 | composite | + `LongTermDebtNoncurrent` →2024-07-31 (10-Q `0001108524-24-000022`)<br>+ `LongTermDebtCurrent` →2024-07-31 (10-Q `0001108524-24-000022`) |
| FY2025Q3 | 2024-10-31 | `revenue` | 9,444,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-08-01→2024-10-31 (10-Q `0001108524-25-000238`) |
| FY2025Q3 | 2024-10-31 | `interest_expense` | 67,000,000 | direct | + `InterestExpenseDebt` 2024-08-01→2024-10-31 (10-Q `0001108524-25-000238`) |
| FY2025Q3 | 2024-10-31 | `shares_outstanding` | 957,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2024-11-27 (10-Q `0001108524-24-000034`) |
| FY2025Q3 | 2024-10-31 | `total_debt` | 8,432,000,000 | composite | + `LongTermDebtNoncurrent` →2024-10-31 (10-Q `0001108524-24-000034`)<br>+ `LongTermDebtCurrent` →2024-10-31 (10-Q `0001108524-24-000034`) |
| FY2025Q4 | 2025-01-31 | `revenue` | 9,993,000,000 | ytd_diff | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-02-01→2025-01-31 (10-K `0001108524-26-000060`)<br>− `RevenueFromContractWithCustomerExcludingAssessedTax` 2024-02-01→2024-10-31 (10-Q `0001108524-25-000238`) |
| FY2025Q4 | 2025-01-31 | `interest_expense` | 68,000,000 | ytd_diff | + `InterestExpenseDebt` 2024-02-01→2025-01-31 (10-K `0001108524-26-000060`)<br>− `InterestExpenseDebt` 2024-02-01→2024-10-31 (10-Q `0001108524-25-000238`) |
| FY2025Q4 | 2025-01-31 | `shares_outstanding` | 961,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-02-28 (10-K `0001108524-25-000006`) |
| FY2025Q4 | 2025-01-31 | `total_debt` | 8,433,000,000 | composite | + `LongTermDebtNoncurrent` →2025-01-31 (10-K `0001108524-26-000060`)<br>+ `LongTermDebtCurrent` →2025-01-31 (10-K `0001108524-26-000060`) |
| FY2026Q1 | 2025-04-30 | `revenue` | 9,829,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-02-01→2025-04-30 (10-Q `0001108524-26-000127`) |
| FY2026Q1 | 2025-04-30 | `interest_expense` | 68,000,000 | direct | + `InterestExpenseDebt` 2025-02-01→2025-04-30 (10-Q `0001108524-25-000030`) |
| FY2026Q1 | 2025-04-30 | `shares_outstanding` | 956,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-05-22 (10-Q `0001108524-25-000030`) |
| FY2026Q1 | 2025-04-30 | `total_debt` | 8,435,000,000 | composite | + `LongTermDebtNoncurrent` →2025-04-30 (10-Q `0001108524-25-000030`)<br>+ `LongTermDebtCurrent` →2025-04-30 (10-Q `0001108524-25-000030`) |
| FY2026Q2 | 2025-07-31 | `revenue` | 10,236,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-05-01→2025-07-31 (10-Q `0001108524-25-000088`) |
| FY2026Q2 | 2025-07-31 | `interest_expense` | 67,000,000 | direct | + `InterestExpenseDebt` 2025-05-01→2025-07-31 (10-Q `0001108524-25-000088`) |
| FY2026Q2 | 2025-07-31 | `shares_outstanding` | 952,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-08-28 (10-Q `0001108524-25-000088`) |
| FY2026Q2 | 2025-07-31 | `total_debt` | 8,436,000,000 | composite | + `LongTermDebtNoncurrent` →2025-07-31 (10-Q `0001108524-25-000088`)<br>+ `LongTermDebtCurrent` →2025-07-31 (10-Q `0001108524-25-000088`) |
| FY2026Q3 | 2025-10-31 | `revenue` | 10,259,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-08-01→2025-10-31 (10-Q `0001108524-25-000238`) |
| FY2026Q3 | 2025-10-31 | `interest_expense` | 67,000,000 | direct | + `InterestExpenseDebt` 2025-08-01→2025-10-31 (10-Q `0001108524-25-000238`) |
| FY2026Q3 | 2025-10-31 | `shares_outstanding` | 937,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2025-11-28 (10-Q `0001108524-25-000238`) |
| FY2026Q3 | 2025-10-31 | `total_debt` | 8,438,000,000 | composite | + `LongTermDebtNoncurrent` →2025-10-31 (10-Q `0001108524-25-000238`)<br>+ `LongTermDebtCurrent` →2025-10-31 (10-Q `0001108524-25-000238`) |
| FY2026Q4 | 2026-01-31 | `revenue` | 11,201,000,000 | ytd_diff | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-02-01→2026-01-31 (10-K `0001108524-26-000060`)<br>− `RevenueFromContractWithCustomerExcludingAssessedTax` 2025-02-01→2025-10-31 (10-Q `0001108524-25-000238`) |
| FY2026Q4 | 2026-01-31 | `interest_expense` | 122,000,000 | ytd_diff | + `InterestExpenseDebt` 2025-02-01→2026-01-31 (10-K `0001108524-26-000060`)<br>− `InterestExpenseDebt` 2025-02-01→2025-10-31 (10-Q `0001108524-25-000238`) |
| FY2026Q4 | 2026-01-31 | `shares_outstanding` | 923,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2026-02-25 (10-K `0001108524-26-000060`) |
| FY2026Q4 | 2026-01-31 | `total_debt` | 14,439,000,000 | composite | + `LongTermDebtNoncurrent` →2026-01-31 (10-Q `0001108524-26-000127`)<br>+ `LongTermDebtCurrent` →2026-01-31 (10-Q `0001108524-26-000127`) |
| FY2027Q1 | 2026-04-30 | `revenue` | 11,133,000,000 | direct | + `RevenueFromContractWithCustomerExcludingAssessedTax` 2026-02-01→2026-04-30 (10-Q `0001108524-26-000127`) |
| FY2027Q1 | 2026-04-30 | `interest_expense` | — | not mapped | |
| FY2027Q1 | 2026-04-30 | `shares_outstanding` | 819,000,000 | nearest | + `EntityCommonStockSharesOutstanding` →2026-05-21 (10-Q `0001108524-26-000127`) |
| FY2027Q1 | 2026-04-30 | `total_debt` | 39,280,000,000 | composite | + `LongTermDebtNoncurrent` →2026-04-30 (10-Q `0001108524-26-000127`)<br>+ `LongTermDebtCurrent` →2026-04-30 (10-Q `0001108524-26-000127`) |

## Filings behind these values

| Accession | Form | Filed |
|---|---|---|
| `0001108524-24-000022` | 10-Q | 2024-08-29 |
| `0001108524-24-000034` | 10-Q | 2024-12-04 |
| `0001108524-25-000006` | 10-K | 2025-03-05 |
| `0001108524-25-000030` | 10-Q | 2025-05-29 |
| `0001108524-25-000088` | 10-Q | 2025-09-04 |
| `0001108524-25-000238` | 10-Q | 2025-12-04 |
| `0001108524-26-000060` | 10-K | 2026-03-02 |
| `0001108524-26-000127` | 10-Q | 2026-05-28 |

## To pin it

1. Read each check above in the named filing (EDGAR full-text or the filing index for the accession).
2. Where the engine is wrong, correct `expected` in `case.json` from the filing — never to make the case pass.
3. Fill in `reviewed` (`by`, `on`, `filings_read` with the accessions read), then move the directory to `tests/corpus/`. The corpus gate then holds the engine to it, and `corpus-real` turns green.
