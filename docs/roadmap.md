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

## Next: v0.2 — real data

1. Harden the EDGAR adapter (`services/ingestion/edgar_adapter.py`): map via
   EdgarTools standardized concepts, handle fiscal-period alignment and
   amended filings; record per-field provenance (which XBRL tag supplied it).
2. Point-in-time storage in DuckDB (`services/normalization/`), keyed by
   (ticker, period, filing date) so restatements are visible, not overwritten.
3. Run the engine over 20–30 real companies; fix the formula assumptions that
   real data breaks (they exist; find them empirically).

## v0.3 — narrative engine, LLM-assisted

- Transcript ingestion (indie-priced transcript APIs exist; check
  redistribution terms before quoting at length).
- LLM layer as *consumer only*: given computed metrics + retrieved passages,
  draft the executive summary and analyst questions with citations to the
  evidence ledger. Hard rule: the LLM may not introduce numbers or claims not
  present in its inputs; validate outputs against the ledger.
- KPI definition-change detection (semantic comparison of KPI definitions
  across periods) — the piece deliberately not faked deterministically in v1.

## v0.4 — calibration (the credibility unlock)

- Delisting-inclusive historical fundamentals (e.g. Sharadar) for
  survivorship-free backtesting.
- Per-sector percentile anchors replacing hand-set anchors.
- Backtest block scores vs forward restatements/underperformance; publish the
  calibration report. Until this exists, the v0-heuristic caveat stays.

## v0.5+ — product surface (only if calibration supports it)

- Batch screening, peer-relative ranking, quarter-over-quarter drift monitor,
  watchlist alerts.
- Web frontend.
- Explicitly out of scope until then: billing, auth, alerts infrastructure.

## Standing constraints

- Financial-sector module is separate future work; banks stay excluded.
- Legal framing rules (docs/legal_framing.md) apply to every new output type.
- Every new formula lands with unit tests + formula_spec entry in the same PR.
