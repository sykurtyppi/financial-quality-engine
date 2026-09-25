# Review: `ko_8k_recast` (DRAFT — `reviewed: null`)

**KO**, CIK 21344, as of 2025-07-31, restatement window from 2024-01-01.

**Why this case exists.** A real 8-K (0000021344-25-000047, filed 2025-06-26) re-presenting 62 prior-period facts at unchanged values: a comparative re-filing is not a revision; quarters derived year-to-date from 10-Qs.

This draft was built offline from the committed trimmed fixture `tests/fixtures/real/companyfacts_KO_trimmed.json` (`scripts/make_corpus_case.py --from-facts`). Its `expected` block is a COPY of what the engine observed; nothing in it has been checked against a filing. The fixture carries no filing index, so the 8-K 4.02 check did not run (`non_reliance` is empty by construction, not by observation).

## What to check, and against which filing

1. `0000021344-25-000047` (8-K, filed 2025-06-26): identify what it re-presents (the payload holds 62 facts from it, for fiscal 2023 and 2024) and why — a segment recast, an accounting change, or exhibits of an S-3/S-8 incorporation. Confirm whether any figure differs from the 10-K it re-presents (`0000021344-25-000011`, filed 2025-02-20).
2. The engine found no revision: every re-presented value equals the original. If the 8-K changed any scored figure by 1% or more, pin that footprint here (not amended: an 8-K is not an `/A`).
3. FY2023Q4 and FY2024Q4 `revenue` and `interest_expense` subtract the 10-Q nine months from the 8-K's annual figure. Check that the 8-K's annual `Revenues` equals the 10-K's (10,849m and 11,544m for the derived quarters).
4. `interest_expense` is read from `InterestExpenseNonoperating` in this window, where the earlier case (as of 2024-06-30) read `InterestExpense`: confirm KO moved to the new tag in its 2024 filings and that the figures are the same concept.
5. The 10-Q/A cover share count from `0000021344-24-000019` still feeds FY2024Q1 `shares_outstanding` (see `ko_10qa_cover_only`).
6. `intangible_assets` is named not inspectable: the selected `FiniteLivedIntangibleAssetsNet` ends 2023-12-31 (1 of 8 quarters filled). Confirm KO reports its intangibles only annually, or under another concept, in 2024–2025.

## What the engine observed

- Tier-1 events: none
- Restatement footprints (raw and derived): none
- Evidence coverage: 92% of scored fields inspected; not inspected: `deferred_revenue` (no series mapped this run), `intangible_assets` (selected series has no period on or after 2024-01-01)
- Fields mapped: 26 (tags in `case.json` → `expected.selections`)

Mapper notes:

- `shares_outstanding`: Share counts matched from cover-page dates within 60 days after quarter end.
- `shares_diluted`: Weighted-average share counts are not additive; quarters without a directly reported value stay missing (no Q4 derivation).

Sample of mapped values and the facts behind them:

| Quarter | Ends | Field | Value | Method | Facts (+ added, − subtracted) |
|---|---|---|---|---|---|
| FY2023Q3 | 2023-09-29 | `revenue` | 11,953,000,000 | direct | + `Revenues` 2023-07-01→2023-09-29 (10-Q `0000021344-24-000060`) |
| FY2023Q3 | 2023-09-29 | `interest_expense` | 368,000,000 | direct | + `InterestExpenseNonoperating` 2023-07-01→2023-09-29 (10-Q `0000021344-24-000060`) |
| FY2023Q3 | 2023-09-29 | `shares_outstanding` | 4,323,413,810 | nearest | + `EntityCommonStockSharesOutstanding` →2023-10-20 (10-Q `0000021344-23-000060`) |
| FY2023Q3 | 2023-09-29 | `total_debt` | 39,954,000,000 | composite | + `LongTermDebtNoncurrent` →2023-09-29 (10-Q `0000021344-23-000060`)<br>+ `LongTermDebtCurrent` →2023-09-29 (10-Q `0000021344-23-000060`)<br>+ `CommercialPaper` →2023-09-29 (10-Q `0000021344-23-000060`) |
| FY2023Q4 | 2023-12-31 | `revenue` | 10,849,000,000 | ytd_diff | + `Revenues` 2023-01-01→2023-12-31 (8-K `0000021344-25-000047`)<br>− `Revenues` 2023-01-01→2023-09-29 (10-Q `0000021344-24-000060`) |
| FY2023Q4 | 2023-12-31 | `interest_expense` | 413,000,000 | ytd_diff | + `InterestExpenseNonoperating` 2023-01-01→2023-12-31 (8-K `0000021344-25-000047`)<br>− `InterestExpenseNonoperating` 2023-01-01→2023-09-29 (10-Q `0000021344-24-000060`) |
| FY2023Q4 | 2023-12-31 | `shares_outstanding` | 4,312,456,168 | nearest | + `EntityCommonStockSharesOutstanding` →2024-02-16 (10-K `0000021344-24-000009`) |
| FY2023Q4 | 2023-12-31 | `total_debt` | 41,716,000,000 | composite | + `LongTermDebtNoncurrent` →2023-12-31 (10-Q `0000021344-24-000017`)<br>+ `LongTermDebtCurrent` →2023-12-31 (10-Q `0000021344-24-000017`)<br>+ `CommercialPaper` →2023-12-31 (8-K `0000021344-25-000047`) |
| FY2024Q1 | 2024-03-29 | `revenue` | 11,300,000,000 | direct | + `Revenues` 2024-01-01→2024-03-29 (10-Q `0000021344-25-000029`) |
| FY2024Q1 | 2024-03-29 | `interest_expense` | 382,000,000 | direct | + `InterestExpenseNonoperating` 2024-01-01→2024-03-29 (10-Q `0000021344-25-000029`) |
| FY2024Q1 | 2024-03-29 | `shares_outstanding` | 4,307,955,307 | nearest | + `EntityCommonStockSharesOutstanding` →2024-04-30 (10-Q/A `0000021344-24-000019`) |
| FY2024Q1 | 2024-03-29 | `total_debt` | 42,218,000,000 | composite | + `LongTermDebtNoncurrent` →2024-03-29 (10-Q `0000021344-24-000017`)<br>+ `LongTermDebtCurrent` →2024-03-29 (10-Q `0000021344-24-000017`)<br>+ `CommercialPaper` →2024-03-29 (10-Q `0000021344-24-000017`) |
| FY2024Q2 | 2024-06-28 | `revenue` | 12,363,000,000 | direct | + `Revenues` 2024-03-30→2024-06-28 (10-Q `0000021344-25-000061`) |
| FY2024Q2 | 2024-06-28 | `interest_expense` | 418,000,000 | direct | + `InterestExpenseNonoperating` 2024-03-30→2024-06-28 (10-Q `0000021344-25-000061`) |
| FY2024Q2 | 2024-06-28 | `shares_outstanding` | 4,309,868,150 | nearest | + `EntityCommonStockSharesOutstanding` →2024-07-25 (10-Q `0000021344-24-000044`) |
| FY2024Q2 | 2024-06-28 | `total_debt` | 43,526,000,000 | composite | + `LongTermDebtAndCapitalLeaseObligations` →2024-06-28 (10-Q `0000021344-24-000044`)<br>+ `LongTermDebtAndCapitalLeaseObligationsCurrent` →2024-06-28 (10-Q `0000021344-24-000044`)<br>+ `CommercialPaper` →2024-06-28 (10-Q `0000021344-24-000044`) |
| FY2024Q3 | 2024-09-27 | `revenue` | 11,854,000,000 | direct | + `Revenues` 2024-06-29→2024-09-27 (10-Q `0000021344-24-000060`) |
| FY2024Q3 | 2024-09-27 | `interest_expense` | 425,000,000 | direct | + `InterestExpenseNonoperating` 2024-06-29→2024-09-27 (10-Q `0000021344-24-000060`) |
| FY2024Q3 | 2024-09-27 | `shares_outstanding` | 4,307,797,138 | nearest | + `EntityCommonStockSharesOutstanding` →2024-10-22 (10-Q `0000021344-24-000060`) |
| FY2024Q3 | 2024-09-27 | `total_debt` | 45,878,000,000 | composite | + `LongTermDebtAndCapitalLeaseObligations` →2024-09-27 (10-Q `0000021344-24-000060`)<br>+ `LongTermDebtAndCapitalLeaseObligationsCurrent` →2024-09-27 (10-Q `0000021344-24-000060`)<br>+ `CommercialPaper` →2024-09-27 (10-Q `0000021344-24-000060`) |
| FY2024Q4 | 2024-12-31 | `revenue` | 11,544,000,000 | ytd_diff | + `Revenues` 2024-01-01→2024-12-31 (8-K `0000021344-25-000047`)<br>− `Revenues` 2024-01-01→2024-09-27 (10-Q `0000021344-24-000060`) |
| FY2024Q4 | 2024-12-31 | `interest_expense` | 431,000,000 | ytd_diff | + `InterestExpenseNonoperating` 2024-01-01→2024-12-31 (8-K `0000021344-25-000047`)<br>− `InterestExpenseNonoperating` 2024-01-01→2024-09-27 (10-Q `0000021344-24-000060`) |
| FY2024Q4 | 2024-12-31 | `shares_outstanding` | 4,301,000,395 | nearest | + `EntityCommonStockSharesOutstanding` →2025-02-18 (10-K `0000021344-25-000011`) |
| FY2024Q4 | 2024-12-31 | `total_debt` | 44,162,000,000 | composite | + `LongTermDebtAndCapitalLeaseObligations` →2024-12-31 (10-Q `0000021344-25-000061`)<br>+ `LongTermDebtAndCapitalLeaseObligationsCurrent` →2024-12-31 (10-Q `0000021344-25-000061`)<br>+ `CommercialPaper` →2024-12-31 (10-Q `0000021344-25-000061`) |
| FY2025Q1 | 2025-03-28 | `revenue` | 11,129,000,000 | direct | + `Revenues` 2025-01-01→2025-03-28 (10-Q `0000021344-25-000029`) |
| FY2025Q1 | 2025-03-28 | `interest_expense` | 387,000,000 | direct | + `InterestExpenseNonoperating` 2025-01-01→2025-03-28 (10-Q `0000021344-25-000029`) |
| FY2025Q1 | 2025-03-28 | `shares_outstanding` | 4,304,266,738 | nearest | + `EntityCommonStockSharesOutstanding` →2025-04-29 (10-Q `0000021344-25-000029`) |
| FY2025Q1 | 2025-03-28 | `total_debt` | 48,738,000,000 | composite | + `LongTermDebtAndCapitalLeaseObligations` →2025-03-28 (10-Q `0000021344-25-000029`)<br>+ `LongTermDebtAndCapitalLeaseObligationsCurrent` →2025-03-28 (10-Q `0000021344-25-000029`)<br>+ `CommercialPaper` →2025-03-28 (10-Q `0000021344-25-000029`) |
| FY2025Q2 | 2025-06-27 | `revenue` | 12,535,000,000 | direct | + `Revenues` 2025-03-29→2025-06-27 (10-Q `0000021344-25-000061`) |
| FY2025Q2 | 2025-06-27 | `interest_expense` | 445,000,000 | direct | + `InterestExpenseNonoperating` 2025-03-29→2025-06-27 (10-Q `0000021344-25-000061`) |
| FY2025Q2 | 2025-06-27 | `shares_outstanding` | 4,303,667,252 | nearest | + `EntityCommonStockSharesOutstanding` →2025-07-22 (10-Q `0000021344-25-000061`) |
| FY2025Q2 | 2025-06-27 | `total_debt` | 49,107,000,000 | composite | + `LongTermDebtAndCapitalLeaseObligations` →2025-06-27 (10-Q `0000021344-25-000061`)<br>+ `LongTermDebtAndCapitalLeaseObligationsCurrent` →2025-06-27 (10-Q `0000021344-25-000061`)<br>+ `CommercialPaper` →2025-06-27 (10-Q `0000021344-25-000061`) |

## Filings behind these values

| Accession | Form | Filed |
|---|---|---|
| `0000021344-23-000060` | 10-Q | 2023-10-24 |
| `0000021344-24-000009` | 10-K | 2024-02-20 |
| `0000021344-24-000017` | 10-Q | 2024-05-02 |
| `0000021344-24-000019` | 10-Q/A | 2024-05-30 |
| `0000021344-24-000044` | 10-Q | 2024-07-29 |
| `0000021344-24-000060` | 10-Q | 2024-10-24 |
| `0000021344-25-000011` | 10-K | 2025-02-20 |
| `0000021344-25-000029` | 10-Q | 2025-05-01 |
| `0000021344-25-000047` | 8-K | 2025-06-26 |
| `0000021344-25-000061` | 10-Q | 2025-07-24 |

## To pin it

1. Read each check above in the named filing (EDGAR full-text or the filing index for the accession).
2. Where the engine is wrong, correct `expected` in `case.json` from the filing — never to make the case pass.
3. Fill in `reviewed` (`by`, `on`, `filings_read` with the accessions read), then move the directory to `tests/corpus/`. The corpus gate then holds the engine to it, and `corpus-real` turns green.
