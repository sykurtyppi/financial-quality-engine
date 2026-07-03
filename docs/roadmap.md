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

## Done (v0.3) — calibration & validation

- Walk-forward backtesting framework: true point-in-time fundamentals (filed-
  date filtering), Yahoo dividend-adjusted relative returns, 8-K Item 4.02
  detection, SIC-based financial-institution auto-exclusion
- 75-company stratified universe (six archetypes + controls + known stress
  cases), 1,275 company-quarter rows, 2021Q1–2025Q1 as-ofs
- Findings: overall score works as a top-quintile tail screen (+10 pp hit-rate
  lift, 44.5% FP rate); accruals predict margin deterioration; capex-regime
  metrics right-signed everywhere; SBC/dilution family wrong-signed in growth
  universes; score bands were compressed and re-anchored to the empirical
  distribution; PTON/SMCI/CVNA carried the sample's top scores at the right
  times; restatement prediction untestable (zero events, survivorship bias)
- Evidence-based reweighting (provenance-commented), two component exclusions,
  reproducibility snapshot tests, committed backtest artifact
- Full account: docs/calibration_report.md

## Done (v0.4) — narrative drift & evidence layer

- EDGAR document ingestion: 10-K/10-Q MD&A + Risk Factors extraction
  (TOC-skip, Unicode normalization, cross-reference-safe boundaries), 8-K
  EX-99 earnings releases with event-date quarter snapping; validated live on
  AAPL/KO/CRM (MD&A 4/4 each)
- New deterministic detectors: guidance shift, defensive tone vs trailing
  baseline, risk-factor expansion (YoY-preferred), high-severity disclosure
  emergence, KPI definition-change candidates (token-similarity)
- QoQ / YoY / trailing-8Q comparison discipline (baselines.py)
- Narrative evidence ledger (NE-nnn ids: detector, period, basis, source,
  verbatim excerpt, linked metrics, confidence) — report §8b
- Four metric-narrative mismatch checks (demand/working-capital,
  profitability/cash-conversion, buyback/share-count, growth-capex/FCF) with
  analyst questions
- LLM grounding contract + validator (unknown ids, banned vocabulary,
  off-vocabulary classifications, ungrounded numbers all rejected; tested)
- Docs: narrative_methodology.md, evidence_policy.md, legal_framing update
- Narrative Drift block remains uncalibrated (no historical document corpus)

## Next: v0.5 — ingestion depth + archetype anchors

1. Point-in-time storage in DuckDB (`services/normalization/`), keyed by
   (ticker, period, filing date) so restatements are visible, not overwritten
   (currently latest-filed silently wins in live mode; the backtester already
   PIT-filters).
2. Wire SIC-based financial-institution auto-exclusion (built for the
   backtester in `backtesting/events.py`) into the live ingestion path.
3. Archetype-aware anchors for the measured false-positive concentrations:
   serial acquirers (70% FP — goodwill/intangibles anchors), healthcare (69%),
   capex-heavy AI infrastructure (62%); the 2026-07 wide sweep added
   regulated utilities (rate-base capex + structural leverage) and defense
   primes (long-term-contract cash timing) as candidates. Spend the
   evaluation window's adjudication labels here (data/sweep/adjudication.csv).
4. IFRS/foreign-private-issuer taxonomy mapping (63 of the top-350 filers are
   outside us-gaap and currently error out with a clear message).
5. Custom-tag/extension mapping for low-coverage sectors (energy first — XOM
   at 63%).

## v0.6 — LLM annotator + document corpus

- Live LLM annotator implementing the grounding contract
  (grounding.validate_annotations already enforces no-invention): drafts the
  executive summary, classifies ledger evidence, and upgrades KPI
  definition-change candidates from token-similarity to semantic comparison.
- Transcript ingestion (indie-priced transcript APIs exist; check
  redistribution terms before quoting at length).
- Build a historical document corpus (EDGAR filings are fetchable
  retroactively) so Narrative Drift can join the next calibration round —
  currently the only untested block.

## v0.7 — full calibration (the credibility unlock)

- Delisting-inclusive historical fundamentals (e.g. Sharadar) to remove the
  survivorship bias that dominates the v0.3 backtest's limitations.
- Per-sector percentile anchors replacing hand-set anchors.
- Restatement prediction testing at a universe size where 8-K 4.02 events
  actually occur; re-run the v0.3 harness unchanged on the bigger sample.
- Until then, the uncalibrated-thresholds caveat stays on every output.

## v0.8+ — product surface (only if calibration supports it)

- Batch screening, peer-relative ranking, quarter-over-quarter drift monitor,
  watchlist alerts.
- Web frontend.
- Explicitly out of scope until then: billing, auth, alerts infrastructure.

## Standing constraints

- Financial-sector module is separate future work; banks stay excluded.
- Legal framing rules (docs/legal_framing.md) apply to every new output type.
- Every new formula lands with unit tests + formula_spec entry in the same PR.
