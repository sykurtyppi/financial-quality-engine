# Earnings Quality & Narrative Drift Engine

A deterministic research engine that analyzes public-company financial
statements and disclosure documents to surface **earnings quality risk,
aggressive presentation, cash-conversion weakness, dilution pressure, capex
regime shifts, and narrative drift** — with every conclusion traceable to a
disclosed formula, its inputs, and its source period.

**This is not an AI filing summarizer and not a fraud-accusation tool.**
Outputs are formula-driven screening opinions that identify items requiring
analyst review. See [docs/legal_framing.md](docs/legal_framing.md).

## Design principle

> Deterministic calculations first. Interpretation second.

Formulas and text analytics run before any interpretive output is generated;
flags, change summaries, and analyst questions are derived exclusively from
computed metrics and matched document evidence. The planned LLM layer
(roadmap) consumes this evidence; it never produces facts.

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
  KPI additions/removals, disclosure volume change

Results roll up into 8 block scores plus an Overall Quality Risk Score
(0–100 concern convention) with exposed weights, anchors, coverage, confidence,
and caveats — see [docs/scoring_methodology.md](docs/scoring_methodology.md).
All weights are **v0 heuristics** and every output says so.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run the test suite (100 tests incl. golden report)
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

### Real SEC data (v0.2)

The engine ingests real EDGAR/XBRL data with no extra dependencies:

```bash
export EDGAR_IDENTITY="Your Name you@example.com"   # SEC fair-access rule
.venv/bin/python - <<'EOF'
from app.services.ingestion.edgar_adapter import fetch_dataset
from app.core.pipeline import analyze
from app.services.reporting.markdown_report import render
from datetime import date

dataset, diagnostics = fetch_dataset("AAPL", n_quarters=8)
print(f"field coverage: {diagnostics.coverage():.0%}")
print(render(analyze(dataset), generated_on=date.today().isoformat()))
EOF
```

The mapper handles the real-world XBRL problems (QTD/YTD duration ambiguity,
missing Q4 flows, cumulative cash-flow items, amendments, filer tag switches,
cover-page share dates, unreliable fy/fp metadata) and reports per-field
provenance and gaps in `IngestionDiagnostics`. It is validated to the dollar
against source filings for AAPL/MSFT/KO — see
[docs/real_data_validation.md](docs/real_data_validation.md) for the full
validation account, verified coverage by sector, and remaining limitations
(notably: energy-sector presentation gaps, and restatement point-in-time
handling is roadmap work).

## Report contents

Executive summary · scorecard · top red/green flags · what changed this
period · narrative & disclosure observations with source snippets · evidence
ledger (formula + inputs per claim) · metric detail including every data gap ·
analyst review questions · disclaimer.

Golden sample: [tests/golden_reports/stretchco_report.md](tests/golden_reports/stretchco_report.md).

## Honest limitations (v0.1)

- Weights/thresholds are uncalibrated heuristics; treat scores as screening
  aids. Calibration plan: [docs/roadmap.md](docs/roadmap.md).
- Sector normalization is a hook, deliberately unpopulated.
- Banks/insurers are excluded by design.
- The academic base rate matters: accounting-screen false-positive rates are
  high in absolute terms; every flag is a review prompt, not a verdict.

## Docs

- **[What this engine can and cannot do](docs/what_this_engine_can_and_cannot_do.md) — capstone: the honest verdict, cross-validated against the literature (start here)**
- [Architecture](docs/architecture.md)
- [Formula specification](docs/formula_spec.md)
- [Scoring methodology](docs/scoring_methodology.md)
- [Legal framing policy](docs/legal_framing.md)
- [Roadmap](docs/roadmap.md)
