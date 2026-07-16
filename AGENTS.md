# AGENTS.md — financial-quality-engine

Per-repo facts for the Hermes review agent (adversarial quant reviewer). This file is the
repo-specific layer under the global SOUL.md identity and the shared six-check review skill
(look-ahead bias, point-in-time violations, survivorship, overfitting/multiple-testing,
cost/execution realism, regime cherry-picking). Report findings as a severity-ranked list
citing exact lines — never a prose summary.

> **Audit provenance.** Verified against `main` at commit `d000720` on 2026-07-16. Package
> versions, line numbers, sample counts, and "currently surviving" observations are
> point-in-time snapshots — re-verify any specific line / number / version against the
> current tree before relying on it. The structural invariants (data providers, risk tier,
> the classes of pitfall) are durable; the exact citations are not.

## Orientation

A **deterministic, rule-based** engine that scores public-company earnings quality and "narrative
drift" from SEC/XBRL data, plus a research program that adversarially stress-tested it and concluded
(honestly, in-repo) that it is a **distress thermometer, not a fraud/failure predictor**. Version
0.4.0; scoring config frozen at 0.3.0. This repo is unusually self-aware — most pitfalls a reviewer
would hunt are already documented under `docs/`. Start from `docs/what_this_engine_can_and_cannot_do.md`.

## 1. Stack & dependencies

- **Python ≥3.12** (`pyproject.toml`). Runtime deps are deliberately tiny: **only `pydantic>=2.7` and
  `fastapi>=0.111`.** **No numpy/pandas/sklearn/scipy/statsmodels — no ML/NLP library.** All numerics are
  hand-rolled Python.
- **Scoring is 100% deterministic**: piecewise-linear interpolation over hand-set anchor tables
  (`app/services/scoring/engine.py::interpolate_concern`), no learned parameters. The one LLM touchpoint is a
  KPI-materiality **judge** (`app/services/narrative/kpi_adjudicator.py::LlmAdjudicator`), provider-agnostic
  via an injected `complete(system, user)` callable, validated against a grounding contract, with a
  deterministic fallback — **and it was ultimately shelved** (see §3).
- **Web**: FastAPI. Entry points `app/main.py` (`/analyze`, `/report`), `app/web.py` (journal UI).
- **Run**: `pytest` (237 tests); `scripts/run_analysis.py data/example_company.json`;
  `scripts/generate_report.py AAPL` with `EDGAR_IDENTITY` env var. Real data = SEC EDGAR/XBRL companyfacts
  (`app/services/ingestion/sec_client.py`, `companyfacts_mapper.py`).

## 2. Risk tier — Research/backtesting (with real point-in-time discipline)

No live capital; grep finds no order-execution or broker/trading code (strong evidence, not exhaustive proof). Outputs are "screening prompts for analyst
review," never predictions or accusations (`docs/legal_framing.md`; every report carries a disclaimer and the
`V0_WEIGHTS_CAVEAT`). Primary risks are **multiple-testing across the sweep, overfitting the scoring
thresholds, and survivorship** — plus a genuine **point-in-time** dimension because it consumes SEC filings
(raw-as-filed vs restated). For any new result, ask whether it holds out-of-sample and whether the sweep
multiplicity was controlled. All scoring/calibration/formula findings are **flag-only**.

## 3. Known pitfalls specific to this repo (the repo's own docs disclose many — treat as leads to verify, not verified findings)

1. **The founding forensic claim is refuted.** `docs/what_this_engine_can_and_cannot_do.md` +
   `docs/restatement_control.md`: single-firm misstatement detection fails (pure-forensic restaters 2/5
   elevated; the textbook MiMedx channel-stuffing case **missed entirely** — Revenue Quality couldn't even
   compute due to a data gap). It detects **distress, not fraud/death** (`docs/distressed_control.md`:
   distressed survivors flag ~70% ≥p90 ≈ decedents' 75%).
2. **The overall score is a tail screen, not a ranking or probability.** `docs/calibration_report.md`:
   top-quintile hit rate 55.5% vs 45.7% base ⇒ **44.5% false-positive rate among flags**; Q1–Q4 non-monotone.
3. **Anchors are fit to the 2021–2025 distribution.** Scores empirically span ~17–67 (not 0–100); direction
   bands were re-anchored to empirical p50/p90 = 32/45 (`engine.py`, `scoring_config.py`). May not generalize;
   the window contains the 2022 rate shock, so the leverage/balance-sheet signal "is partly a rate-cycle effect
   and may not generalize" (regime-cherry-picking risk, cited in `calibration_report.md`).
4. **Multiple-testing across the sweep is uncorrected — mitigated only by refusing to claim significance.**
   `data/sweep/` yields 29 flags across 215 names; the block×outcome IC table (8 blocks × 3 outcomes +
   component ICs) is scanned without correction, but the report explicitly declines to quote significance
   ("no IC standard errors … would be false precision at this n"). Sweep multiplicity concentrates in
   `scripts/generate_calibration_snapshot.py` and `scripts/wide_sweep.py`.
5. **Thresholds were deliberately NOT tuned to the backtest** to avoid overfitting the small sample
   (`calibration_report.md`) — only block weights, two exclusions, and direction bands changed. Credit this;
   flag any change that starts fitting anchors to the backtest.
6. **Survivorship — "the single largest limitation."** Calibration universe is currently-listed tickers;
   partially addressed by `docs/survivorship_pilot.md` (delisted firms by CIK, 10/12 flagged pre-death) but the
   missing control is distressed *survivors* (see #1).
7. **Wrong-signed components excluded/descriptive-only.** `buyback_offset_ratio` and
   `working_capital_swing_to_income` are weight-0 (wrong-signed); Capital Integrity/SBC is wrong-signed in
   growth universes, kept "descriptive only" (`scoring_config.py`).
8. **Narrative Drift block is fully uncalibrated** — no historical document corpus in the backtest; all anchors
   are pure judgment. The 100% narrative hit rate on restaters is **not** discrimination
   (`adjustment_recurrence`/`disclosure_reduction` are filing-length artifacts; no clean-company control run).
9. **KPI-definition-drift signal SHELVED.** Looked predictive (3/6) but `docs/kpi_definition_isolation_spike.md`
   showed it was mostly extraction/windowing artifacts (~1 in 10 genuinely recoverable); hit its pre-committed
   STOP criterion.
10. **Point-in-time IS genuinely enforced — verify changes don't break it.** `app/services/backtesting/pit.py::
    filter_as_of` keeps only facts with `filed ≤ as_of` and **drops facts lacking a `filed` date**;
    `companyfacts_mapper.py::_dedupe_latest_filed` is latest-filed-wins per period (the key restatement-leakage
    control — in PIT mode it dedupes only over facts filed ≤ as-of). Outcomes are *allowed* to see the future by
    design (`backtesting/outcomes.py`), which is correct — don't flag that as leakage.
11. **Restatement "catches" can be contemporaneous, not predictive.** The narrative `before=` cutoff is the
    8-K Item-4.02 announcement date, so material-weakness language filed just before counts as a catch though it
    isn't predictive (`docs/restatement_narrative.md`).
12. **The bundled example is SYNTHETIC** (`data/example_company.json` = "StretchCo" from `generate_golden.py`) —
    don't treat the golden report as real-data evidence. Coverage artifacts: low field coverage (e.g. energy
    63–75%) suppresses scores, so a low score can be a coverage artifact, not an all-clear. Financial
    institutions are excluded by SIC (`pipeline.py`). Beneish M-score is estimated on annual data but applied
    quarterly, with documented LVGI/TATA deviations (`docs/formula_spec.md`).
13. **A full blind, pre-registered, survivorship-complete RCT is DESIGNED but NOT IMPLEMENTED**
    (`docs/blind_validation_framework.md` is "methodology only"). There is no executed human-subjects validation.

## 4. What the agent may fix directly vs only flag

**Default posture is read-only.** During a review-only task, report proposed changes as
findings and do not edit; post inline PR comments only as the configured review bot or when
explicitly asked, not merely because a PR exists. Fixes apply only when explicitly
authorized — and even then, numerical / signal / statistical changes require focused
before/after validation and human review, never a silent edit. "Low-risk" is not risk-free:
UI, scheduler, deploy, CORS, and DB code can still be consequential — treat every item below
as a candidate, not standing authorization.


**Flag only — never auto-fix (scoring / calibration / numerical judgment; changes void the frozen 0.3.0 eval
and require the reproducibility-snapshot diff — cite `docs/evaluation_protocol.md`):**
`app/config/scoring_config.py` (all anchors/weights/direction bands/exclusions), `app/services/scoring/engine.py`
(interpolation/aggregation, coverage/confidence thresholds), `app/services/formulas/*` (Beneish coefficients,
ratio definitions), `app/services/narrative/*` detector logic (term lists, KPI dictionaries),
`app/services/backtesting/pit.py` / `outcomes.py` / `runner.py` (the PIT/look-ahead boundary).

**Low-risk — only if a fix is explicitly requested, (infra / API / style, no numerical effect):**
`app/api/routes.py`, `app/main.py`, `app/web.py`, `app/templates/*`, reporting/markdown rendering;
`app/services/ingestion/sec_client.py` networking/caching/retry, logging, error handling; test scaffolding;
`pyproject.toml` warning filter; docs typos.

**Guardrail:** `tests/integration/test_calibration_reproducibility.py` mechanically blocks any scoring-config
change from landing without a reviewed snapshot diff. **A diff touching `tests/golden_reports/
calibration_snapshot.json` means scoring logic changed — treat it as a red flag and review the numbers.**

## 5. PR etiquette

Findings as **inline review comments on exact lines**, severity-ranked, framed as reasons a score could
mislead (sweep multiplicity, uncalibrated narrative anchors, survivorship, contemporaneous-not-predictive
catches). No full-file rewrites unless explicitly asked to push a fix commit. For flag-only areas, comment and
stop. Because the repo already admits most limitations, prioritize findings that are *new* or that a change
would *reintroduce* (e.g. breaking PIT, tuning anchors to the backtest).
