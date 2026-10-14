# Review: `wageworks_q3_2017_10qa` (reviewed 2026-10-14)

**WageWorks, Inc.**, CIK 1158863, as of 2019-04-26, restatement window from 2017-09-30.

**Why this case exists.** WageWorks's Q3 2017 10-Q/A restated multiple scored financial figures, and its April 5, 2018 8-K reported that the Q3 2017 financial statements should no longer be relied upon. The case must produce Tier 1 evidence; a clean result is a false clean.

## Filings reviewed on SEC EDGAR

- `0001158863-17-000026` — original Q3 2017 10-Q, filed 2017-11-08.
- `0001193125-18-108845` — 8-K filed 2018-04-05, including Item 4.02 non-reliance.
- `0001158863-19-000010` — 2017 10-K filed 2019-03-18, including the restatement background and restated 2017 quarterly data.
- `0001158863-19-000044` — Q3 2017 10-Q/A filed 2019-04-26.

## Review result

The 10-Q/A states that the Q3 2017 balance sheet, three- and nine-month income statements, and nine-month cash flows were restated. Its as-previously-reported, adjustment, and as-restated tables confirm every material field-period pair pinned in `case.json`:

- total assets: $1,649.556m to $1,625.876m;
- stock-based compensation, nine months: $25.096m to $17.988m;
- selling and general/administrative expense composition, nine months: materially reduced;
- receivables: $183.237m to $159.489m;
- property and equipment, net: $69.963m to $68.417m;
- operating income: $16.821m to $16.048m for Q3 and $46.291m to $54.980m year to date;
- net income: $9.685m to $9.063m for Q3 and $39.887m to $45.398m year to date;
- interest expense: $1.987m to $2.059m for Q3 and $5.047m to $5.261m year to date;
- deferred revenue: $7.107m to $6.374m;
- current liabilities: $793.616m to $779.564m;
- current assets: $1,097.333m to $1,073.109m;
- operating cash flow: $121.889m to $129.117m.

The filing also confirms the Q3 stock-based-compensation change underlying the derived-quarter footprint: $8.072m to $7.176m.

The amendment changed Q3 revenue from $115.746m to $114.846m and nine-month revenue from $360.627m to $358.015m. Those changes are below the engine's documented 1% restatement materiality threshold, so the absence of a revenue footprint is consistent with the current rule rather than an unchecked omission. The sub-1% cash and accounts-payable changes are treated the same way.

The 8-K expressly identifies Item 4.02 and says the affected 2016 and 2017 financial statements should no longer be relied upon. The expected `non_reliance` accession is therefore correct.

No `expected` value required correction. The expected outcome remains `tier1: true`, with the amended material footprints and Item 4.02 event recorded in `case.json`.
