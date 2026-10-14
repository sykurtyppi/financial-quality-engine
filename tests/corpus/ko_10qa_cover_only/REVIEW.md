# Review: `ko_10qa_cover_only` (reviewed 2026-10-14)

**KO**, CIK 21344, as of 2024-06-30, restatement window from 2023-01-01.

**Why this case exists.** A real 10-Q/A (0000021344-24-000019, filed 2024-05-30) that re-files only the cover share count: an amendment with no scored impact must not reach Tier 1.

This case was built offline from the committed trimmed fixture `tests/fixtures/real/companyfacts_KO_trimmed.json` (`scripts/make_corpus_case.py --from-facts`). Its expectations were reviewed against the SEC EDGAR filings listed below on 2026-10-14. The fixture carries no filing index, so the 8-K 4.02 check does not run; `non_reliance` remains empty because this case is scoped to the cover-only 10-Q/A behavior, not as evidence that no Item 4.02 filing exists.

## Review result

1. The 2024-05-30 10-Q/A says it was filed solely to add omitted iXBRL tagging to Part II, Item 5. It expressly says no other disclosure was modified or updated. It contains no amended income statement, balance sheet, or cash-flow statement.
2. Both the original 2024-05-02 10-Q and the 10-Q/A report 4,307,955,307 shares outstanding as of 2024-04-30. The amendment therefore creates no amended share-count footprint.
3. The 2023 10-K reports full-year net operating revenue of $45,754m and interest expense of $1,527m. The Q3 2023 10-Q reports nine-month values of $34,905m and $1,114m. Their differences are $10,849m and $413m, matching the case's FY2023Q4 derivations. The 10-K does not separately disclose those two fourth-quarter figures in a fourth-quarter note.
4. The SEC companyfacts payload contains neither `ContractWithCustomerLiabilityCurrent` nor `DeferredRevenueCurrent`; leaving `deferred_revenue` uninspected is correct for this case.

No `expected` value required correction. The expected outcome remains `tier1: false` with no restatement footprint.

## What to check, and against which filing

1. `0000021344-24-000019` (10-Q/A, filed 2024-05-30): confirm it is an amendment of the Q1 2024 10-Q (`0000021344-24-000017`, filed 2024-05-02) and what it changed. The companyfacts payload carries ONE fact from it: the cover-page share count 4,307,955,307 dated 2024-04-30. Confirm no income-statement, balance-sheet or cash-flow figure was amended.
2. The engine maps FY2024Q1 `shares_outstanding` from that 10-Q/A cover (the payload holds no cover count from the original 10-Q). Check that the original 10-Q's cover reported a count, and whether it equals 4,307,955,307. If it differs by 1% or more, this case must pin an amended `shares_outstanding` footprint and the engine's clean reading is a false clean; if equal (or absent), the expected `tier1: false` stands.
3. FY2023Q4 `revenue` 10,849m and `interest_expense` 413m are derived as the 10-K year (`0000021344-24-000009`) minus the 10-Q nine months (`0000021344-23-000060`). Check both figures against the 10-K's fourth-quarter note, if it gives one.
4. `deferred_revenue` is named not inspectable. Confirm KO does not report `ContractWithCustomerLiabilityCurrent` (or the engine's other candidates) in this window.

## What the engine observed

- Tier-1 events: none
- Restatement footprints (raw and derived): none
- Evidence coverage: 96% of scored fields inspected; not inspected: `deferred_revenue` (no series mapped this run)
- Fields mapped: 26 (tags in `case.json` → `expected.selections`)

Mapper notes:

- `shares_outstanding`: Share counts matched from cover-page dates within 60 days after quarter end.
- `shares_diluted`: Weighted-average share counts are not additive; quarters without a directly reported value stay missing (no Q4 derivation).

Sample of mapped values and the facts behind them:

| Quarter | Ends | Field | Value | Method | Facts (+ added, − subtracted) |
|---|---|---|---|---|---|
| FY2023Q2 | 2023-06-30 | `revenue` | 11,972,000,000 | direct | + `Revenues` 2023-04-01→2023-06-30 (10-Q `0000021344-23-000048`) |
| FY2023Q2 | 2023-06-30 | `interest_expense` | 374,000,000 | direct | + `InterestExpense` 2023-04-01→2023-06-30 (10-Q `0000021344-23-000048`) |
| FY2023Q2 | 2023-06-30 | `shares_outstanding` | 4,324,344,812 | nearest | + `EntityCommonStockSharesOutstanding` →2023-07-25 (10-Q `0000021344-23-000048`) |
| FY2023Q2 | 2023-06-30 | `total_debt` | 41,440,000,000 | composite | + `LongTermDebtNoncurrent` →2023-06-30 (10-Q `0000021344-23-000048`)<br>+ `LongTermDebtCurrent` →2023-06-30 (10-Q `0000021344-23-000048`)<br>+ `CommercialPaper` →2023-06-30 (10-Q `0000021344-23-000048`) |
| FY2023Q3 | 2023-09-29 | `revenue` | 11,953,000,000 | direct | + `Revenues` 2023-07-01→2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q3 | 2023-09-29 | `interest_expense` | 368,000,000 | direct | + `InterestExpense` 2023-07-01→2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q3 | 2023-09-29 | `shares_outstanding` | 4,323,413,810 | nearest | + `EntityCommonStockSharesOutstanding` →2023-10-20 (10-Q `0000021344-23-000060`) |
| FY2023Q3 | 2023-09-29 | `total_debt` | 39,954,000,000 | composite | + `LongTermDebtNoncurrent` →2023-09-29 (10-Q `0000021344-23-000060`)<br>+ `LongTermDebtCurrent` →2023-09-29 (10-Q `0000021344-23-000060`)<br>+ `CommercialPaper` →2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q4 | 2023-12-31 | `revenue` | 10,849,000,000 | ytd_diff | + `Revenues` 2023-01-01→2023-12-31 (10-K `0000021344-24-000009`)<br>− `Revenues` 2023-01-01→2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q4 | 2023-12-31 | `interest_expense` | 413,000,000 | ytd_diff | + `InterestExpense` 2023-01-01→2023-12-31 (10-K `0000021344-24-000009`)<br>− `InterestExpense` 2023-01-01→2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q4 | 2023-12-31 | `shares_outstanding` | 4,312,456,168 | nearest | + `EntityCommonStockSharesOutstanding` →2024-02-16 (10-K `0000021344-24-000009`) |
| FY2023Q4 | 2023-12-31 | `total_debt` | 41,716,000,000 | composite | + `LongTermDebtNoncurrent` →2023-12-31 (10-Q `0000021344-24-000017`)<br>+ `LongTermDebtCurrent` →2023-12-31 (10-Q `0000021344-24-000017`)<br>+ `CommercialPaper` →2023-12-31 (10-Q `0000021344-24-000017`) |
| FY2024Q1 | 2024-03-29 | `revenue` | 11,300,000,000 | direct | + `Revenues` 2024-01-01→2024-03-29 (10-Q `0000021344-24-000017`) |
| FY2024Q1 | 2024-03-29 | `interest_expense` | 382,000,000 | direct | + `InterestExpense` 2024-01-01→2024-03-29 (10-Q `0000021344-24-000017`) |
| FY2024Q1 | 2024-03-29 | `shares_outstanding` | 4,307,955,307 | nearest | + `EntityCommonStockSharesOutstanding` →2024-04-30 (10-Q/A `0000021344-24-000019`) |
| FY2024Q1 | 2024-03-29 | `total_debt` | 42,218,000,000 | composite | + `LongTermDebtNoncurrent` →2024-03-29 (10-Q `0000021344-24-000017`)<br>+ `LongTermDebtCurrent` →2024-03-29 (10-Q `0000021344-24-000017`)<br>+ `CommercialPaper` →2024-03-29 (10-Q `0000021344-24-000017`) |

## Filings behind these values

| Accession | Form | Filed |
|---|---|---|
| `0000021344-23-000048` | 10-Q | 2023-07-27 |
| `0000021344-23-000060` | 10-Q | 2023-10-24 |
| `0000021344-24-000009` | 10-K | 2024-02-20 |
| `0000021344-24-000017` | 10-Q | 2024-05-02 |
| `0000021344-24-000019` | 10-Q/A | 2024-05-30 |

## To pin it

1. Read each check above in the named filing (EDGAR full-text or the filing index for the accession).
2. Where the engine is wrong, correct `expected` in `case.json` from the filing — never to make the case pass.
3. Fill in `reviewed` (`by`, `on`, `filings_read` with the accessions read), then move the directory to `tests/corpus/`. The corpus gate then holds the engine to it, and `corpus-real` turns green.
