# Validation corpus

Every review round so far has found defects the test suite could not see.
The tests were the author's own examples, and the engine agreed with them by
construction. The corpus is the opposite. It holds real filings, chosen for a
reason. Each case's expectations are pinned from the engine's **observed**
output, and only after a person has read the filings and agrees with them.

Code: `app/services/corpus.py`. Gate: `tests/integration/test_corpus.py`.
Cases: `tests/corpus/<name>/`.

## What a case is

```
tests/corpus/<name>/
  companyfacts.json   # trimmed: only the concepts the engine reads
  submissions.json    # trimmed: the filing index as of the case date
  case.json           # ticker, cik, as_of, since, why, reviewed, expected
```

`expected` holds the following fields:

- `tier1`: whether the card must show at least one Tier-1 event.
- `footprints`: every revision the restatement check must find. Each entry
  records whether a `/A` amendment is behind it.
- `non_reliance`: the accessions of 8-K Item 4.02 filings inside the card's
  window.
- `selections`: which concept each field must be read from.
- `coverage_min`: the share of scored fields the revision check must inspect.
- `uninspected`: fields the report must name as *not inspectable*, so it
  never gives a clean bill of health it cannot back.

## The gates

Every test run checks the following. The summary line prints each metric and
the number of **real** cases.

| Metric | Gate | Meaning |
|---|---|---|
| False-clean rate | 0 | A case with a pinned signal must never read clean |
| Restatement recall | 1.0 | Every pinned footprint is found; a pinned amendment needs a `/A` behind it |
| Amended precision | 1.0 | Every amendment the check promotes to Tier 1 is pinned |
| Evidence coverage | ≥ `coverage_min`, per case | Fields that cannot be inspected are named |

Cases marked `synthetic` are self-tests of the harness. They show that the
harness can fail in both directions: one fires, one must stay quiet. They are
never evidence. While the corpus holds only synthetic cases, the summary says
`NOT validation`.

A separate CI check, `corpus-real`, fails while no reviewed real case is
pinned (`FQE_REQUIRE_REAL_CORPUS=1` enables
`test_the_corpus_holds_real_cases`). It is red on purpose until the first
real case lands. It is not part of the required `test` check, so it never
blocks a merge. Its job is to stop a green corpus from being read as a
validated engine.

## Adding a case (needs SEC access)

```
EDGAR_IDENTITY="Name email" .venv/bin/python scripts/make_corpus_case.py \
    <name> <TICKER> [--cik N] --as-of YYYY-MM-DD --since YYYY-MM-DD --why "..."
```

1. The script fetches, trims and runs the engine as of the case date. It then
   writes a **draft** `case.json` whose expectations are copied from what it
   observed, with `reviewed: null`, and prints that observation. The corpus
   test refuses a draft.
2. Read the filings the observation names, and any the engine should have
   named but did not. Correct `expected` wherever the engine is wrong. A
   missed amendment is added by hand, from the filing; that is what the
   corpus is for. Leave the case failing until the engine is fixed.
3. Fill in `reviewed`: who read the filings, the date, and the accessions
   read.

Never type expectations from memory, and never edit an expectation to make a
case pass.

## Draft cases from the committed fixtures (no SEC access needed)

`tests/corpus_drafts/` holds four real-filer drafts built offline from the
trimmed AAPL, KO and CRM fixtures:

| Draft | What it exercises |
|---|---|
| `ko_10qa_cover_only` | A real 10-Q/A that re-files only the cover share count; must not reach Tier 1 |
| `ko_8k_recast` | A real 8-K re-presenting 62 prior-period facts at unchanged values; a tag move for interest expense |
| `aapl_comparative_rounding` | Fourth quarters derived as fiscal year minus nine months; a comparative re-filed at different rounding on an unscored tag |
| `crm_january_fye` | A January fiscal year end; composed SG&A; a proxy fact in the payload |

Each has a `REVIEW.md` listing what to check and which accession to read.
The gate never reads them. `tests/integration/test_corpus_drafts.py` checks
only that they are still unreviewed and that the engine still observes what
they recorded. To pin one, work through its `REVIEW.md`, correct `expected`,
fill in `reviewed`, and move the directory to `tests/corpus/`. The first one
pinned turns `corpus-real` green.

None of the committed fixtures holds a scored 10-Q/A or 10-K/A revision, an
8-K 4.02, a same-day conflict, or a discontinued-operations re-presentation,
and none derives a quarter as fiscal year minus three quarters. Those cases
still need a fresh EDGAR pull (next section).

## Cases to add

Confirm each accession on EDGAR before pinning. Each case exists for one
reason:

| Case | Why |
|---|---|
| WageWorks 10-Q/A (see `restatement_control.md`) | An amendment that moved scored figures: Tier 1 must fire |
| A 10-Q/A filed only to add an exhibit | An amendment with no scored impact: Tier 1 must **not** fire |
| Kraft Heinz 8-K 4.02 | A non-reliance event inside the window |
| XOM receivables tag migration | A taxonomy move is not a revision |
| NVDA same-day 8-K + 10-Q | `filed == as_of` visibility |
| NVDA 2024 stock split | Split-adjusted share counts are excluded, not revised |
| ASC 842 finance-lease adoption | A composition change, not a revision |
| CRM (52/53-week calendar) | Fiscal labels and quarter ends |
| A custom-tag issuer (`data_artifact?` rows in `data/sweep/adjudication.csv`) | Uninspected fields are named; no clean bill |
