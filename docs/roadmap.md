# Roadmap

## Done (v0.1)

- Deterministic formula engine (40+ metrics, metric contract, guards)
- Beneish component family + M-score with documented deviations
- Deterministic narrative analytics: adjustment recurrence, KPI dictionary
  drift, disclosure volume
- Scoring engine: 8 blocks + overall, exposed v0 weights/anchors, coverage,
  confidence, high-growth and financial-institution false-positive controls
- Markdown reporting with evidence ledger and non-removable disclaimer
- FastAPI endpoints, JSON ingestion, CLI, 100-test suite incl. golden report

## Done (v0.2) — real-data hardening

- Replaced edgartools dependency with a dependency-free SEC companyfacts
  client + pure offline-testable mapper (durations, Q4 derivation, YTD
  differencing, amendment dedupe, coverage-scored tag selection, composite
  SG&A/D&A/debt, cover-date share matching, structural fiscal labels)
- Validated against 8 cross-sector companies (63–99% field coverage);
  11 real-data bug classes found, fixed, and regression-tested
- Verified to the dollar against source filings for AAPL, MSFT, KO
  (spot values + annual reconciliation of derived quarterlies)
- Real public-domain fixtures committed (AAPL/KO/CRM — three fiscal-calendar
  shapes); report §7 now distinguishes data-unavailable / not-meaningful /
  concern / caveat
- Full account: docs/real_data_validation.md

## Next: v0.3 — remaining ingestion depth

1. Point-in-time storage in DuckDB (`services/normalization/`), keyed by
   (ticker, period, filing date) so restatements are visible, not overwritten
   (currently latest-filed silently wins).
2. SIC-code lookup (SEC submissions API) to auto-flag financial institutions
   instead of relying on the caller.
3. Custom-tag/extension mapping for low-coverage sectors (energy first — XOM
   at 63%); utilities and REITs untested.
4. Widen the sweep to 20–30 companies including small caps and recent IPOs.

## v0.4 — narrative engine, LLM-assisted

- Transcript ingestion (indie-priced transcript APIs exist; check
  redistribution terms before quoting at length).
- LLM layer as *consumer only*: given computed metrics + retrieved passages,
  draft the executive summary and analyst questions with citations to the
  evidence ledger. Hard rule: the LLM may not introduce numbers or claims not
  present in its inputs; validate outputs against the ledger.
- KPI definition-change detection (semantic comparison of KPI definitions
  across periods) — the piece deliberately not faked deterministically in v1.

## v0.5 — calibration (the credibility unlock)

- Delisting-inclusive historical fundamentals (e.g. Sharadar) for
  survivorship-free backtesting.
- Per-sector percentile anchors replacing hand-set anchors.
- Backtest block scores vs forward restatements/underperformance; publish the
  calibration report. Until this exists, the v0-heuristic caveat stays.

## v0.6+ — product surface (only if calibration supports it)

- Batch screening, peer-relative ranking, quarter-over-quarter drift monitor,
  watchlist alerts.
- Web frontend.
- Explicitly out of scope until then: billing, auth, alerts infrastructure.

## Standing constraints

- Financial-sector module is separate future work; banks stay excluded.
- Legal framing rules (docs/legal_framing.md) apply to every new output type.
- Every new formula lands with unit tests + formula_spec entry in the same PR.
