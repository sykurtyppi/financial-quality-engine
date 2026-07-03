# Wide Sweep Summary — 2026-07-03

Config 0.3.0 (frozen) · `scripts/wide_sweep.py 350` · fundamentals only (no documents)
Artifacts: `sweep_results.csv` (full), `adjudication.csv` (29 flags awaiting analyst labels)

## Coverage

| Outcome | Count | Notes |
|---|---|---|
| Scored | 215 | overall score produced |
| Excluded (financial, SIC auto) | 47 | banks/insurers correctly kept out |
| Errors — foreign private issuers | 63 | IFRS filers (ASML, SHEL, NVS, SAP, …): no us-gaap facts; known taxonomy boundary, candidate v0.5+ work |
| Errors — network / no facts | 16 | retryable / thin filers |
| Scored + no overall | 9 | insufficient block coverage — reported, not fabricated |

## Score distribution (n=215)

min 18.4 · p50 33.8 · p80 43.1 · p90 45.8 · max 61.5 — closely matches the
v0.3 calibration distribution (p50 31.7 / p90 45.1): the scoring is stable
out of sample. Flag rate above the negative band (>45): 29 companies (13.5%).

## The embarrassing-false-positive hunt: what it found

The 29 flags cluster into recognizable groups (adjudication is the analyst's,
but the patterns are measurable):

1. **Regulated utilities (SO, DUK, CEG, ETR — 4 of 29)**: structurally
   negative FCF/NI and net debt/EBITDA of 4-6x IS the regulated-utility
   business model (rate-base capex funded with debt). These look like the
   strongest new systematic-FP archetype. Candidate treatments: sector
   anchors or exclusion-with-note, decided AFTER adjudication, post-window.
2. **Defense primes (LMT, NOC, LHX — 3 of 29)**: long-term-contract
   percentage-of-completion accounting makes CFO/NI lumpy and
   receivables/contract assets structurally heavy. Likely industry_normal;
   same treatment path as utilities.
3. **Capex-heavy AI/infra (AMZN and others)**: the FP pattern already
   measured in v0.3 (62% FP rate), reproduced here.
4. **Refiner/commodity working-capital swings (PSX)**: price-driven
   inventory/receivables moves; likely industry_normal.
5. **Residual review candidates (e.g. QCOM — Earnings Quality top block via
   accrual trend; RKLB — Revenue Quality via receivables spread; PM —
   leverage + inventory build)**: no obvious structural excuse; these are the
   flags worth an analyst's hour and are the sweep's actual product.

Zero flags were auto-labeled `data_artifact?` (all flagged names had ≥60%
field coverage) — flags are being driven by computed values, not data gaps.

## Next actions (per docs/evaluation_protocol.md)

- Analyst adjudicates the 29 rows in `adjudication.csv` (taxonomy in the
  protocol). Utilities/defense hypotheses above are hints, not pre-labels.
- Run the narrative/document layer on adjudicated `genuine_concern` names
  (the funnel's expensive stage).
- Config stays frozen; archetype-anchor changes happen once, at window close,
  spending the adjudication labels.
