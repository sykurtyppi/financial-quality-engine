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
(≤200 characters); `note` ≤500; a scenario's `fcf_growth` in `(−100%, +100%]`
(the bisection bracket's top — past it a present value overflows a double);
no control character (a newline, a tab, a C1 control, …) and no Unicode
line or paragraph separator (NEL U+0085, U+2028, U+2029) anywhere in
`source`, `note` or a scenario name, since these are emitted into the report
and a line break would let the file write a heading there (the renderer
also escapes `#`, `-`, `*`, `>`, `+` where a line could start, and `|`); the
ticker by the journal's file-name rules. The reader additionally refuses a
file whose `observed_at` or `recorded_at` is after the clock: the model
alone cannot know the time, and a file dated 2999 throughout is consistent
but not an observation. A file that is present but is not a valid
observation **fails the report build closed** (`NotPublished`, naming the
file; `generate_report.py` exits 3) — a report must not go live while its
one market datum is in doubt. The file is never read or written through a
symlink.

**Days are EDGAR's.** A filing is dated by its US/Eastern calendar day, so the
observation's own day — `MarketObservation.eastern_day`, on which both its
availability cut and its age are counted — is its `America/New_York` calendar
day, not the UTC one (23:30 Eastern is already the next day in UTC). Age is
`eastern_today − eastern_day`, clamped at 0, where `eastern_today` is the
build clock's `America/New_York` day (`observation.eastern_today`) — not
`generated_on`, the host-local date that anchors the streams (on a UTC host
an evening observation read "age 1 day" on the card and 0 in `market.py
show`). The card names the day it counted on. An observation more than
`STALE_AFTER_DAYS = 7` days old on that day is marked **STALE** on the card
(the filing facts are as of the report; the price is not).

## The period the facts come from: as filed by the observation

The report's dataset is latest-filed-wins over the whole payload, so an FY-end
quarter carries the date of the 10-Q that later repeated it as a comparative,
and a restated figure is the restated one. Neither is what an observation
before that later filing could have seen, and a check on those dates skipped
the 10-K's own period (review of 48b1f04, F1). So the plane does not read the
report's dataset when it has the raw companyfacts payload (every report entry
point passes it): it maps its own dataset through the mapper's point-in-time
cut — `pit.build_pit_dataset`, i.e. `build_dataset(as_of=)`, the backtests'
boundary, imported and not changed — with the same window length and profile
as the report's, as of

    as_filed_by = eastern_day(observed_at) − 1 day

(`bridge.available_through`). The cut is inclusive (`filed ≤ as_of`), and a
filing dated the observation's own Eastern day counts as **not yet
available**: EDGAR accepts a filing at any hour of its day, and a close at
16:00 with a 10-Q accepted at 17:30 is the common case. The card's filing
block opens with the sentence `filing-derived facts as filed by <date>
(filings dated <day> treated as not yet available): <period>, ending <end>`,
and every filing row cites the as-filed accession and filing date; the ledger
records the sentence in `valuation.availability`.

Within that dataset the bridge takes the **latest period that carries its
inputs** (a share count — either kind — total debt and cash); a later period
short of one is skipped and named on the sentence with the missing field. With
no complete period the latest is used and the EV line names what it lacks.
When fewer than two quarter ends were filed by the cut the mapper refuses, no
period is used and EV is not asserted (`no period can be built from the facts
filed by <date>`). A restated figure therefore shows its original value while
the observation predates the amendment and the amended one after it; the
TTM figures and the ledger's TTM rows come from the same as-filed dataset. At
or after the newest filing the as-filed dataset is the report's own, period
for period and source for source — what the drill's step 13 rehearses.

A caller with the dataset alone (no raw facts: the API's, a synthetic one)
cannot check availability. Its latest complete period is used and the
sentence says so — `filing availability at the observation date not checked
(no raw facts): latest period <label> (ending <end>) used` — never a guess
dressed as a check.

## The enterprise-value bridge

| Line | Formula / source | Class | Not asserted when |
|---|---|---|---|
| price | the observation | `[O]` | — |
| share count | `shares_outstanding` (dei cover-page count, labelled with its cover date); else `shares_diluted` (weighted-average diluted, labelled as such) | `[F]` | neither reported for the period |
| market cap | price × share count | `[D]` | share count missing; the price not in USD (below) |
| total debt | `total_debt` (incl. finance leases, excl. operating leases — the engine's own definition) | `[F]` | missing → **EV not asserted: total_debt missing for `<period>`** |
| cash and equivalents | `cash_and_equivalents` | `[F]` | missing → EV not asserted |
| short-term investments | `short_term_investments` | `[F]`, or `[A]` "not reported (assumed 0)" | — |
| minority interest | `minority_interest` | `[F]` or `[A]` assumed 0 | — |
| preferred stock | `preferred_stock` | `[F]` or `[A]` assumed 0 | — |
| **enterprise value** | market cap + total debt − cash − short-term investments + minority interest + preferred stock | `[D]` | the price not in USD; any of share count, debt, cash missing (the reason names the field and period) |
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

**Currency.** Every monetary fact is read in USD (`fields.FILING_CURRENCY`,
the companyfacts unit the mapper collects; a fact filed only in another
currency is not read). Nothing converts a currency. A price recorded in any
other currency (the CLI's `market.py record --currency` accepts one; the
workbench's price box records USD only) is shown as recorded, the filing
lines are shown as they are, and nothing that would mix the two is
asserted: market cap and EV say **EV not asserted: price in EUR, filing
figures in USD — no FX conversion**, and every multiple, both implied
growths and every scenario carry that reason instead of a number, on the
card and in the ledger (whose valuation summary records it as `ev_reason`).
The card, the flags and every score are the same as without the price
(Hermes audit of PR #118, finding 2: a EUR price was multiplied by the share
count and added to USD debt). Every monetary figure on the card says its
currency: the price the one it was recorded in, every filing figure and
everything derived from them USD; a share count is a count.

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

**The solves are against the market cap, not EV.** `FCF_ttm` is the engine's
FCF (CFO − capex): after interest, a flow to *equity*. Equating its present
value to EV would set a levered flow against an unlevered value and count the
debt twice (review of 48b1f04, F7). EV serves the EV multiples only; an
unlevered FCF / NOPAT reading is phase 2 (below).

| Quantity | Formula | Not computable when |
|---|---|---|
| Gordon implied perpetual FCF growth | `g = r − FCF_ttm / market cap` | FCF ≤ 0; market cap not built (share count missing); TTM window incomplete |
| reverse two-stage DCF implied growth | the `g` with `Σ_{t=1..H} FCF_ttm(1+g)^t/(1+r)^t + FCF_ttm(1+g)^H(1+g_T)/((r−g_T)(1+r)^H) = market cap`, by bisection over `[−99%, +100%]` (PV is increasing in `g`; deterministic, no numpy) | as above; no solution in the bracket (said as "below −99%/yr" / "above +100%/yr"); a present value past a double ("overflow (present value not finite)") |
| sensitivity | implied growth at `r ± 1pt` and at price ± 10% (market cap × 0.9 and × 1.1); the swing is the larger distance from the base case. A move that cannot be valued — `r − 1pt` not above `g_T` (or not above 0), `r + 1pt` not below 100%, or an end of the move outside the bracket — is printed as **withheld** with the reason, never dropped | base case not computable (then no sensitivity line at all) |
| the main assumption that would change the conclusion | the valued sensitivity with the larger swing, named; the withheld ones named after it (`… (price ± 10% withheld)`), so a one-sided answer never reads as the whole; "none: every sensitivity withheld" when neither is valued | no sensitivities |
| scenario value per share | `PV(FCF_ttm at the scenario's growth for its years, then its terminal growth, at its r) / share count` — an equity value, no bridge claims taken off — vs the price (% upside/downside); each tagged `model assumption: <name>` | FCF ≤ 0; share count missing; terminal growth ≥ r; a present value past a double |

No scenarios recorded → the card says "no scenarios recorded".

## Explicit v1 limits

- one observation per ticker, no price history, no own-history range;
- no peer reference class;
- no NOPAT, no tax normalisation, no invested capital — EBIT is the operating
  income the engine maps, EBITDA adds its D&A, FCF is CFO − capex as the
  engine's `fcf_margin` reads them, and because that FCF is levered every
  implied-growth solve and scenario is an equity-side one (against the market
  cap, per share); an unlevered FCF or NOPAT against EV is phase 2;
- no forward estimates of any kind, and **no PEG, ever** (it needs licensed
  consensus estimates: a data purchase and out of scope);
- no currency conversion: the filing figures are USD, unscaled, and a price
  in any other currency derives nothing (see **Currency** above);
- a replay (`--as-of`) never carries an observation: the price is of today.

## The non-scoring guarantee

The plane reads the raw facts (through the point-in-time cut), the dataset
and the observation, and never the analysis result. `build_report` with and
without the observation produces a byte-identical decision card, thermometer,
result and every non-valuation ledger item; only the appendix section and the
`Plane.VALUATION` rows differ.
Pinned by `tests/unit/test_valuation_report.py::test_card_scores_and_every_other_ledger_item_are_byte_identical`
(on the three real fixtures, with and without the evidence streams) and
rehearsed by step 13 of the earnings-night drill. In the ledger every valuation
row is `UNVALIDATED`: the plane is unscored, so no tier of the card ranks it and
nothing in it was measured against an outcome. The price row rests on
`kind="observation"` provenance — which carries the observation's four fields
and none of a filing's or a snapshot's, with aware timestamps, as the schema
enforces in both directions — and which the review console shows as
"market observation · `<source>` · observed `<ts>`" with no EDGAR link and no
tick: it is not reconcilable to a filing. The ledger's `valuation` summary
has two states, "produced" and "not produced: no market observation": a plane
that cannot be computed fails the build closed and no ledger is written.

## Deliberately deferred

- **Phase 2** (per the review): normalised operating earnings, NOPAT, invested
  capital, ROIC and incremental ROIC, the reinvestment rate, a residual-income
  valuation; composing the operating-lease split; tax and pre-tax fields.
- **Phase 3**: journaling whether the valuation plane changed a decision (the
  decision-impact journal's question, asked of this plane).
