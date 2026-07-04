# Earnings Quality & Narrative Drift Engine

A deterministic, evidence-grounded engine that scores public-company financial
statements and disclosures — then a research program that stress-tested it
against real SEC data until it revealed the honest limits of what such a tool
can do.

Every conclusion is traceable to a disclosed formula, its inputs, and its
source period. **This is not an AI filing summarizer and not a fraud-accusation
tool** — outputs are formula-driven screening prompts for analyst review (see
[docs/legal_framing.md](docs/legal_framing.md)).

## The headline finding

The engine was built to be a forensic earnings-quality detector. Six
point-in-time historical controls on real EDGAR data — then cross-checked
against the academic literature — established what it can and cannot do:

| Capability | Verdict | Evidence |
|---|---|---|
| **Distress detection** (is this firm under stress now?) | **works** | 75–83% of firms that later failed flagged ≥p90 vs a 13.5% base rate |
| **Failure prediction** (which distressed firms actually die?) | **does not** | distressed-survivors flag 70% ≥p90 — the same as decedents |
| **Single-firm accounting-misstatement detection** | **does not** | pure-forensic restaters: 2/5 elevated; the textbook channel-stuffing case missed |
| **High-severity-disclosure monitor** | **works, but contemporaneous** | 30% of restaters vs 0% of clean peers — fires *with* the disclosure, not ahead |

Those results independently reproduce 25 years of accounting and finance
research: distress is detectable (Altman 1968) but failure-conditional-on-distress
is not (Campbell-Hilscher-Szilagyi 2008); single-firm fraud screens have low
precision at real base rates (Beneish 1999; Dechow et al. 2011; Bao et al. 2020,
and the Walker 2021 critique); and predictive accounting signals decay after
publication (McLean-Pontiff 2016; Green-Hand-Soliman 2011). A signal can be real
on average across thousands of firms (firm-level return R² ≈ 2%) yet near-useless
for classifying one company — which is the regime a per-company screen operates in.

**→ Start with the capstone:
[docs/what_this_engine_can_and_cannot_do.md](docs/what_this_engine_can_and_cannot_do.md)**
— the full verdict with the six controls mapped to the literature, and honest
caveats (small n, discovered-fraud bias, point-in-time discipline).

The engineering is real and works; the value of the project is that it says,
with evidence, what these tools actually do rather than what they are marketed
to do.

## Design principle

> Deterministic calculations first. Interpretation second.

Formulas and text analytics run before any interpretive output is generated;
flags, change summaries, and analyst questions are derived exclusively from
computed metrics and matched document evidence. The LLM layer (a materiality
adjudicator, see below) consumes this evidence under a grounding contract — it
never produces facts.

## What it computes

- **Accruals & cash reality**: Sloan-style total accruals, CFO/NI, FCF/NI,
  FCF margin, accrual trend
- **Beneish family**: all 8 components + M-score (documented deviations in
  [docs/formula_spec.md](docs/formula_spec.md))
- **Working capital / revenue quality**: receivables/inventory/deferred-revenue
  growth spreads, DSO/DIO/DPO and trends, working-capital dependency
- **Balance sheet stress**: net debt/EBITDA, coverage, current ratio, asset
  quality proxy, intangibles share, leverage change
- **Capital structure**: SBC/revenue, SBC/CFO, dilution, buyback offset,
  issuance pressure
- **Capex regime**: intensity, growth spread, capex/D&A, regime shift,
  incremental revenue per capex dollar
- **Narrative drift** (deterministic text analytics): recurring
  "one-time"/restructuring language across 8–12 quarters with source snippets,
  KPI additions/removals, disclosure volume change, high-severity-disclosure
  emergence

Results roll up into 8 block scores plus an Overall Quality Risk Score
(0–100 concern convention) with exposed weights, anchors, coverage, confidence,
and caveats — see [docs/scoring_methodology.md](docs/scoring_methodology.md).
All weights are **v0/v0.3 heuristics** and every output says so.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run the test suite (237 tests incl. golden report + calibration snapshot)
.venv/bin/pytest

# Analyze the bundled example company and print the markdown report
.venv/bin/python scripts/run_analysis.py data/example_company.json

# Or run the API
.venv/bin/uvicorn app.main:app --reload
# POST a CompanyDataset JSON to /analyze (JSON result) or /report (markdown)
```

Input format: the JSON serialization of `CompanyDataset`
(`app/schemas/financials.py`) — company profile, ≥ 2 chronological periods of
normalized statement data, and optional per-period documents (earnings
releases, MD&A, transcripts) for the narrative engine.

### Real SEC data

The engine ingests real EDGAR/XBRL data with no extra dependencies:

```bash
export EDGAR_IDENTITY="Your Name you@example.com"   # SEC fair-access rule
.venv/bin/python scripts/generate_report.py AAPL
```

The mapper handles the real-world XBRL problems (QTD/YTD duration ambiguity,
missing Q4 flows, cumulative cash-flow items, amendments, filer tag switches,
cover-page share dates, unreliable fy/fp metadata) and reports per-field
provenance and gaps in `IngestionDiagnostics`. It is validated to the dollar
against source filings for AAPL/MSFT/KO — see
[docs/real_data_validation.md](docs/real_data_validation.md) for verified
coverage by sector and remaining limitations (notably energy-sector
presentation gaps).

Point-in-time discipline (companyfacts filed-date filtering + document `before=`
cutoffs) is used throughout the backtests, so no result depends on hindsight.

## How it was validated

The engine was not just built and demoed — it was subjected to adversarial
historical controls, each documented honestly (including the failures):

- [survivorship_pilot](docs/survivorship_pilot.md) — delisted companies flagged pre-death vs base rate
- [distressed_control](docs/distressed_control.md) — the correction: distress ≠ death
- [restatement_control](docs/restatement_control.md) — the forensic claim, refuted on its own cases
- [restatement_narrative](docs/restatement_narrative.md) / [clean_narrative_control](docs/clean_narrative_control.md) — which narrative detectors actually discriminate
- [narrative_timing](docs/narrative_timing.md) — early-warning vs contemporaneous
- [kpi_llm_validation](docs/kpi_llm_validation.md) + [kpi_definition_isolation_spike](docs/kpi_definition_isolation_spike.md) — the KPI-drift signal, and why it was shelved
- A decision-impact journal ([journal/JOURNAL.md](journal/JOURNAL.md)) tests the one open question no historical run can answer: does surfacing the validated signals change a real decision?

## LLM layer (grounded materiality adjudicator)

The one place an LLM is used is as a *judge*, not an author: given the prior and
current definition of one non-GAAP metric, it rules whether the change is
material. It is blind to outcomes, its output is validated against a grounding
contract (no banned vocabulary, no ungrounded numbers, quoted clauses must be
substrings of the source), it is cached, and it degrades to a deterministic
fallback on any failure. Phase 4 kept the adjudicator (it improved precision)
but shelved the underlying signal — see
[docs/kpi_drift_llm_design.md](docs/kpi_drift_llm_design.md) and
[docs/kpi_llm_validation.md](docs/kpi_llm_validation.md).

## Honest limitations

- Weights/thresholds are v0/v0.3 heuristics; treat scores as screening aids, not
  verdicts. Every flag is a review prompt.
- The score is a **distress thermometer**, validated as triage — not a predictor
  of failure or fraud, and not a tradeable edge (distressed stocks historically
  *underperform*; Campbell et al. 2008).
- Historical controls use small samples (n = 10–16); the *direction* matches the
  large-sample literature, but specific percentages are noisy.
- Sector normalization is a deliberately unpopulated hook; banks/insurers are
  excluded by design.

## Docs

- **[What this engine can and cannot do](docs/what_this_engine_can_and_cannot_do.md) — capstone: the honest verdict, cross-validated against the literature (start here)**
- [Architecture](docs/architecture.md)
- [Formula specification](docs/formula_spec.md)
- [Scoring methodology](docs/scoring_methodology.md)
- [Narrative methodology](docs/narrative_methodology.md)
- [Legal framing policy](docs/legal_framing.md)
- [Roadmap](docs/roadmap.md)

## License

[MIT](LICENSE) © 2026 Tristan Alejandro. Research and educational tool; not
investment advice. See the disclaimer in every generated report and
[docs/legal_framing.md](docs/legal_framing.md).
