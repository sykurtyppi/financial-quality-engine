# Evidence Policy

## The rule

**Every narrative claim the engine emits must be traceable to a ledger entry**
containing: a stable evidence id (`NE-nnn`), the detector that produced it,
the fiscal period, the comparison basis (`qoq` / `yoy` / `trailing8` /
`point`), the source document, a verbatim excerpt, any linked deterministic
metrics, and a confidence level. Claims without ledger entries do not exist
as far as reports are concerned.

Financial claims follow the parallel metric contract: formula + inputs +
period in the metric evidence ledger (§8a of the report). Narrative evidence
is §8b. Metric-narrative mismatches must reference both sides.

## Confidence vocabulary

- **high** — multiple corroborating observations (e.g. a term recurring across
  ≥6 periods; a mismatch with concern ≥70 and repeated narrative emphasis;
  high-severity disclosure terms, which are near-unambiguous).
- **medium** — single-basis detection or lexicon candidates (definition
  changes, tone shifts, guidance stance).
- **low** — reserved for degraded inputs (sparse documents); prefer reporting
  a data gap over a low-confidence claim.

## LLM grounding contract (`app/services/narrative/grounding.py`)

A future LLM annotator may **explain and classify** ledger evidence. Its
output must be `GroundedAnnotation` objects, and `validate_annotations`
rejects (never repairs):

1. **Unknown evidence ids** — every annotation cites ≥1 real `NE-nnn`.
2. **Off-vocabulary classifications** — only: requires_review,
   elevated_concern, narrative_drift, presentation_risk,
   industry_normal_candidate, model_artifact_candidate, supportive.
3. **Banned vocabulary** — the legal-framing banned list (fraud,
   manipulation, …).
4. **Ungrounded numbers** — any number in the explanation must appear in the
   cited excerpts/details or linked metric values. Numbers from nowhere are
   the canonical hallucination.

Rejected annotations are dropped with logged reasons. There is no partial
acceptance and no automatic rewriting — a failed validation is a signal the
annotator prompt or model needs fixing, not the validator.

## What this buys

- Reports are auditable: every sentence can be traced to a computation or an
  excerpt.
- The LLM layer can be added without weakening the no-invention guarantee,
  because the guarantee lives in a validator with tests, not in a prompt.
- Legal posture: claims are verifiable computations plus quoted disclosure,
  framed as review prompts (docs/legal_framing.md).
