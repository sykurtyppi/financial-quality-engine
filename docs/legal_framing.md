# Legal Framing Policy

This engine screens for **earnings quality and presentation risk**. It must
never accuse. This is both an ethical position and a liability firewall:
quantitative claims framed as verifiable fact lose opinion protection in
defamation analysis, and research publishers naming companies have been sued
by their targets. Framing rules are therefore enforced in code and tests, not
just style guidance.

## Hard rules

1. **Banned vocabulary in all generated output**: "fraud", "fraudulent",
   "manipulation"/"manipulating", "deceptive", "cooking the books",
   "engineered beat", or any phrasing asserting intent or wrongdoing.
   The integration suite asserts generated flags and questions contain none of
   these (see `test_pipeline.py::test_no_accusatory_language`).
2. **Approved vocabulary**: "elevated earnings quality risk", "aggressive
   presentation", "cash conversion weakness", "recurring adjustment concern",
   "narrative drift", "warrants review", "requires analyst review".
3. **Every claim is a disclosed computation**: each flag cites its metric,
   formula, inputs, and period. Nothing is asserted that cannot be recomputed
   from the evidence ledger.
4. **Scores are labeled screening opinions**, not probabilities of misconduct.
   The v0-heuristic caveat and the report disclaimer are non-removable parts
   of the output contract.
5. **Missing data is stated**, never papered over — an incomplete analysis
   presented as complete is itself a misrepresentation.

## Report disclaimer

The standing disclaimer lives in
`app/services/reporting/markdown_report.py::DISCLAIMER` and appears in every
report. It states: automated formula-driven analysis of public data; opinions,
not allegations; not investment advice; elevated scores are review prompts,
not conclusions; heuristic thresholds.

## Note names, not intent

Good: "Receivables grew 38% while revenue grew 11%; cash conversion and
revenue quality require review."

Bad: "The company is inflating revenue."

The first is a verifiable computation plus a review prompt. The second is an
assertion of intent the data cannot support.

## Regulatory posture (informational, not legal advice)

- Impersonal, general-circulation research publications have Investment
  Advisers Act "publishers' exclusion" precedent; person-specific investment
  advice does not. Keep outputs impersonal and general.
- If outputs are ever published per-company at scale, obtain a legal review of
  the framing and disclaimer before launch. This document is engineering
  policy, not counsel.
