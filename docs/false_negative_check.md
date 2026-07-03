# False-Negative Miss Test (Track 2)

Date: 2026-07-03 · Config: 0.3.0 (frozen) · Harness: `scripts/miss_test.py`

Question: did the screen elevate BEFORE publicly known accounting/quality
blowups? Point-in-time data (facts filed ≤ as-of only). Score bands from the
v0.3 calibration distribution (p50 31.7 / p80 40.3 / p90 45.1; 2021–2025
sample — pre-2021 comparisons are indicative, not exact).

## Results

| Case | Event | Pre-event score (as-of) | Band | What drove it | Verdict |
|---|---|---|---|---|---|
| KHC | Feb-2019 $15B writedown, SEC subpoena, restatement | **52.4** (2018-08) | ≥p90 | Cash Conversion 93, Balance Sheet 62 | **Caught** |
| UAA | Late-2016 growth break; SEC probe into 2015-16 revenue pull-forwards | **56.3** (2016-05) | ≥p90 | Cash Conversion 89, **Revenue Quality 75**, 8 red flags | **Caught** — the revenue-quality block flagged the exact issue later probed |
| PLUG | Mar-2021 non-reliance (4.02), FY2018-20 restatement | **47.4** (2020-08) | ≥p90 | Cash Conversion 82, Earnings Quality 60 | **Caught** |
| SMCI | Aug-2018 delinquency → Nov-2018 non-reliance | **43.9** (2017-08); 2017-11 as-of = **STALE FILINGS** | ≥p80 + staleness | Cash Conversion 86 | **Caught** — the stale-filing detector fired on the actual delinquency in real time |
| GE | Oct-2017 insurance reserve shortfall, $6.2B charge, SEC probe | 36.3–39.0 (2017) | p50–p80 | Cash Conversion 68; coverage only 63% | **Missed (soft)** — above median but not elevated |
| TUP | 2023 going concern, restatement | — | — | ticker no longer in SEC registry (delisted 2024) | **Untestable** — survivorship bias in action |

## Honest interpretation

- **4 of 5 testable cases were elevated (≥p80, mostly ≥p90) before the event**,
  each driven by the blocks the v0.3 calibration found most trustworthy
  (cash conversion, earnings/revenue quality). UAA is the standout: Revenue
  Quality 75 four months before the growth break, three years before the SEC
  probe became public.
- **GE is an instructive miss**: its blowup came from insurance reserve
  adequacy inside GE Capital — a liability-estimation problem these formulas
  do not measure, compounded by conglomerate mapping gaps (63% coverage).
  Lesson: the screen's blind spots are (a) financial-arm risks and
  (b) low-coverage presentations — both already documented as limitations.
- **Selection caveats**: six famous cases chosen by the author, not a random
  sample; base rates cannot be inferred (the wide sweep addresses the other
  side — how often clean companies score this high). Delisted blowups remain
  untestable with free data, which is precisely the v0.7 calibration gap.
- The pre-event windows were chosen before scoring (dates in the script),
  not cherry-picked after seeing results; per-case only the two fixed as-ofs
  were run.
