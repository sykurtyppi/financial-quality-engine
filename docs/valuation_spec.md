# Valuation shadow card specification (v1)

The engine scores accounting quality, distress, dilution and disclosure. It does
not answer the separate investment question — *what expectations are embedded in
the current price, and are they reasonable?* The external review of `02c2aac`
(Hermes) recommended keeping the engine as the evidence layer and adding an
**optional valuation and expectations plane above it, never putting a multiple
into a score.** This is phase 1 of that plane: a "shadow card" appended to the
report's appendix and ledgered on its own plane. It is a lens on the price, not
a verdict, and it moves nothing the engine scores.

Code: `app/services/valuation/` (`observation`, `bridge`, `multiples`,
`expectations`, `render`, `plane`); the one input is recorded with
`scripts/market.py`. The card is rendered by `report_builder.build_report`
only when a caller asked for it (`generate_report.py`, `journal.py report`,
the watch); the API and `run_analysis.py` never produce it.

## The three data classes

Every line of the card carries one tag, and the three are never mixed into
one authoritative-looking number:

| Tag | Class | Where it comes from | Example |
|---|---|---|---|
| `[F]` | filing-derived fact | the mapped dataset, with the filed facts behind each value (`PeriodFinancials.sources`) | total debt, the cover-page share count, TTM FCF |
| `[O]` | market observation | the operator's record (`journal/market/<T>.json`): price, exact timestamp with offset, what was looked at | `price 182.40 USD observed 2026-11-18T21:00:00+00:00` |
| `[A]` | model assumption | the observation file's `assumptions` / `scenarios`, or the documented defaults, labelled as defaults | required return 9.0%; "short-term investments not reported (assumed 0)" |
| `[D]` | derived | arithmetic over the lines above | market cap, EV, every multiple, every implied growth |

## The observation

One per ticker, recorded once (no history). Validated on the way in:
price finite and positive; currency three upper-case ASCII letters;
`observed_at` timezone-aware and not after `recorded_at`; `source` non-empty
(≤200 characters); `note` ≤500; the ticker by the journal's file-name rules.
A file that is present but is not a valid observation **fails the report build
closed** (`NotPublished`, naming the file; `generate_report.py` exits 3) — a
report must not go live while its one market datum is in doubt. The file is
never read or written through a symlink. An observation more than
`STALE_AFTER_DAYS = 7` days older than the report's day is marked **STALE** on
the card (the filing facts are as of the report; the price is not).

## The period the facts come from

The latest period of the dataset whose bridge facts were all **filed on or
before the observation's day** (the filing dates behind the period's share
count, debt, cash and the optional lines). A period re-filed after the
observation (a comparative in a later 10-Q, an amendment) is skipped and
named; an observation earlier than every filing asserts no EV. A dataset
without per-value provenance (the API's, a synthetic one) cannot be checked:
its latest period is used and the card says the check was not made.

## The enterprise-value bridge

| Line | Formula / source | Class | Not asserted when |
|---|---|---|---|
| price | the observation | `[O]` | — |
| share count | `shares_outstanding` (dei cover-page count, labelled with its cover date); else `shares_diluted` (weighted-average diluted, labelled as such) | `[F]` | neither reported for the period |
| market cap | price × share count | `[D]` | share count missing |
| total debt | `total_debt` (incl. finance leases, excl. operating leases — the engine's own definition) | `[F]` | missing → **EV not asserted: total_debt missing for `<period>`** |
| cash and equivalents | `cash_and_equivalents` | `[F]` | missing → EV not asserted |
| short-term investments | `short_term_investments` | `[F]`, or `[A]` "not reported (assumed 0)" | — |
| minority interest | `minority_interest` | `[F]` or `[A]` assumed 0 | — |
| preferred stock | `preferred_stock` | `[F]` or `[A]` assumed 0 | — |
| **enterprise value** | market cap + total debt − cash − short-term investments + minority interest + preferred stock | `[D]` | any of share count, debt, cash missing (the reason names the field and period) |
| operating lease liabilities | `operating_lease_liabilities` — **shown, never in EV** (lessee comparability caveat) | `[F]` | — |
| stockholders' equity (book) | `stockholders_equity` — shown, not a bridge component | `[F]` | — |

The five balance-sheet fields on the right are mapped by the companyfacts
mapper like any other (`fields.py`, `scored=False`): with provenance, point in
time, and in the selection snapshot — but **no metric reads them**, the
field-coverage figure, the restatement scan and the silent-revision diff are
over the scored fields only, and `pit.py` trims to the scored set exactly. A
filer that reports only the operating-lease split (current + noncurrent) maps
no aggregate and the line says "not reported" (composing the split is deferred:
the per-quarter resolver is flow-only and the debt composer is total-debt-only).

## Multiples

TTM figures are the engine's own (`formulas/ttm.annualize` over the four
consecutive quarters ending at the bridge's period): revenue, net income, EBIT,
EBITDA (EBIT + D&A), FCF (CFO − capex).

| Multiple | Formula | Not meaningful when |
|---|---|---|
| P/E | market cap / TTM net income | net income ≤ 0 or missing |
| EV/EBIT | EV / TTM EBIT | EBIT ≤ 0 or missing; EV not asserted |
| EV/EBITDA | EV / TTM EBITDA | EBITDA ≤ 0 or missing; EV not asserted |
| EV/Sales | EV / TTM revenue | revenue ≤ 0 or missing; EV not asserted |
| P/S | market cap / TTM revenue | revenue ≤ 0 or missing |
| P/FCF | market cap / TTM FCF | FCF ≤ 0 or missing |
| FCF yield | TTM FCF / market cap | FCF missing (a negative yield is shown as a number) |
| earnings yield | TTM net income / market cap | net income missing (likewise) |

Every multiple is also not meaningful when the TTM window has fewer than four
consecutive quarters or the market cap could not be built; the reason is on the
line, e.g. `TTM net income is negative (-1,200): P/E undefined`. This is how the
card agrees with `thesis_monitor_architecture.md`: earnings multiples are
undefined on exactly the distressed names the engine is validated on, and here
they are shown as undefined, with the reason, rather than as a number.

Own-history range and peer range are **not available** in v1 and are said to
be: one price observation, no price history recorded; no reference class
defined. They are never approximated.

## Implied expectations (all `[A]` or `[D]`)

Assumptions `r` (required return), `g_T` (terminal growth), `H` (horizon,
years) come from the observation file's `assumptions`; absent, the defaults
`r = 0.09`, `g_T = 0.025`, `H = 10` are used and every line that uses them says
**default assumptions (not operator-supplied)**. `g_T < r` is enforced.

| Quantity | Formula | Not computable when |
|---|---|---|
| Gordon implied perpetual FCF growth | `g = r − FCF_ttm / EV` | FCF ≤ 0; EV ≤ 0 or not asserted; TTM window incomplete |
| reverse two-stage DCF implied growth | the `g` with `Σ_{t=1..H} FCF_ttm(1+g)^t/(1+r)^t + FCF_ttm(1+g)^H(1+g_T)/((r−g_T)(1+r)^H) = EV`, by bisection over `[−99%, +100%]` (PV is increasing in `g`; deterministic, no numpy) | as above; or no solution in the bracket (said as "below −99%/yr" / "above +100%/yr") |
| sensitivity | implied growth at `r ± 1pt` and at price ± 10% (EV ± 10% of market cap); the swing is the larger distance from the base case | base case not computable |
| the main assumption that would change the conclusion | the sensitivity with the larger swing, named | no sensitivities |
| scenario value per share | `(PV(FCF_ttm at the scenario's growth for its years, then its terminal growth, at its r) − (EV − market cap)) / share count`, vs the price (% upside/downside); each tagged `model assumption: <name>` | FCF ≤ 0; share count missing; EV not asserted; terminal growth ≥ r |

No scenarios recorded → the card says "no scenarios recorded".

## Explicit v1 limits

- one observation per ticker, no price history, no own-history range;
- no peer reference class;
- no NOPAT, no tax normalisation, no invested capital — EBIT is the operating
  income the engine maps, EBITDA adds its D&A, FCF is CFO − capex as the
  engine's `fcf_margin` reads them;
- no forward estimates of any kind, and **no PEG, ever** (it needs licensed
  consensus estimates: a data purchase and out of scope);
- no currency conversion: the price's currency is shown beside it and the
  filing figures are in the filer's reporting currency, unscaled;
- a replay (`--as-of`) never carries an observation: the price is of today.

## The non-scoring guarantee

The plane reads the dataset and the observation and never the analysis
result. `build_report` with and without the observation produces a
byte-identical decision card, thermometer, result and every non-valuation
ledger item; only the appendix section and the `Plane.VALUATION` rows differ.
Pinned by `tests/unit/test_valuation_report.py::test_card_scores_and_every_other_ledger_item_are_byte_identical`
(on the three real fixtures, with and without the evidence streams) and
rehearsed by step 13 of the earnings-night drill. In the ledger every valuation
row is `UNVALIDATED`: the plane is unscored, so no tier of the card ranks it and
nothing in it was measured against an outcome. The price row rests on
`kind="observation"` provenance, which the review console shows as
"market observation · `<source>` · observed `<ts>`" with no EDGAR link and no
tick: it is not reconcilable to a filing.

## Deliberately deferred

- **Phase 2** (per the review): normalised operating earnings, NOPAT, invested
  capital, ROIC and incremental ROIC, the reinvestment rate, a residual-income
  valuation; composing the operating-lease split; tax and pre-tax fields.
- **Phase 3**: journaling whether the valuation plane changed a decision (the
  decision-impact journal's question, asked of this plane).
