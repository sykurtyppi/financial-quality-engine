# Architecture

## Design principle

**Deterministic calculations first. Interpretation second.**

Every narrative statement the system produces (flags, changes, analyst
questions) is generated *after* and *from* computed metrics or retrieved
document evidence. There is no path through the pipeline where a conclusion is
produced without a traceable metric or text match behind it. The planned LLM
layer (roadmap) is a *consumer* of computed evidence, never a producer of facts.

## Data flow

```
CompanyDataset (profile + periods + documents)
        │
        ├── services/formulas/registry.compute_metrics()
        │       └── 40+ MetricResults, each with formula, inputs, status
        │
        ├── services/narrative/narrative_metrics.compute_narrative_metrics()
        │       └── deterministic text analytics → MetricResults + NarrativeFindings
        │
        ├── services/scoring/engine.score_all()
        │       └── 8 BlockScores + OverallScore (weights, anchors, coverage exposed)
        │
        └── core/pipeline.analyze()
                └── AnalysisResult (flags, changes, evidence ledger, questions)
                        │
                        └── services/reporting/markdown_report.render()
                                └── analyst-grade markdown report
```

## Layers

| Layer | Location | Responsibility |
|---|---|---|
| Schemas | `app/schemas/` | Pydantic contracts: financial data, metric results, scores, report |
| Ingestion | `app/services/ingestion/` | Canonical JSON loader; optional EDGAR adapter (network-dependent) |
| Formulas | `app/services/formulas/` | Pure, testable formula functions under the metric contract |
| Narrative | `app/services/narrative/` | Deterministic text analytics: adjustment recurrence, KPI drift, disclosure volume |
| Scoring | `app/services/scoring/` + `app/config/scoring_config.py` | Concern mapping, block/overall scores, caveats |
| Pipeline | `app/core/pipeline.py` | Orchestration, exclusion rules, flag/evidence generation |
| Reporting | `app/services/reporting/` | Deterministic markdown rendering |
| API | `app/api/` + `app/main.py` | FastAPI endpoints: `/analyze`, `/report`, `/health` |

## The metric contract

Every formula returns a `MetricResult` with:

- `value` and `status` (`ok` / `missing_data` / `not_meaningful`),
- the exact `formula` string and all raw `inputs`,
- `missing_fields` when data is absent,
- a `note` when a guard fired (e.g. negative net income makes CFO/NI meaningless).

Missing data is **reported, never silently dropped** — the report's Metric
Detail section lists every metric that failed to compute and why.

## Exclusions

Financial institutions (`profile.is_financial_institution = true`) are excluded
from analysis with an explicit exclusion report: accrual, working-capital, and
leverage formulas are not meaningful for bank/insurer balance sheets.

## Key invariants (enforced by tests)

1. No formula raises on bad data; it degrades to an explicit status.
2. A block with insufficient data scores `None`, never a fabricated midpoint.
3. The v0-heuristic caveat is attached to every score.
4. Reports are byte-deterministic for identical inputs (golden-file tested).
5. Output language never uses accusatory terms (tested against a banned-word list).
