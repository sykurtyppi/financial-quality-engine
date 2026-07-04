"""Hardened, structured KPI-definition extraction (design stage 1).

Produces evidence-ID'd `KpiDefinition` records per period and candidate
`KpiDefinitionPair` changes, feeding the KPI-drift signal. Improvements over the
original single-sentence `kpi_drift.detect_definition_changes`:

- MULTI-SENTENCE definitions: a definition that continues across sentences
  ("...is defined as X. It also excludes Y and Z.") is captured whole.
- TABLE-BASED definitions: non-GAAP reconciliation context is captured as a
  fallback when no prose definition exists.
- MISSING definitions: a KPI touted without any definition is reported
  (status = mentioned_no_definition), never silently dropped.
- RENAMED KPIs: a dropped KPI whose prior definition strongly overlaps a newly
  introduced KPI is flagged as a possible rename (best-effort).
- EVIDENCE IDS: every extracted definition and every candidate pair carries a
  stable id, so the downstream ledger + grounding validator can reference it.

Token-similarity is used here ONLY as a cheap pre-filter to drop unchanged
pairs; materiality judgment is deferred to the adjudicator (design stage 3).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.schemas.financials import DocumentRecord
from app.services.narrative.baselines import group_documents
from app.services.narrative.kpi_drift import (
    DEFINITION_CUES,
    KPI_DICTIONARY,
    _token_similarity,
)

CONTINUATION_CUES = (
    "exclud", "including", "includes", "plus", "less ", "net of", "adjusted for",
    "consists of", "comprises", "represents", "calculated", "as well as", "together with",
)
RECON_CUES = ("reconciliation of", "reconciled to", "reconciliation to", "non-gaap reconciliation")
MAX_DEF_SENTENCES = 4
PREFILTER_SIMILARITY = 0.90  # pairs at/above this are unchanged; not candidates
RENAME_OVERLAP = 0.40


class KpiDefinition(BaseModel):
    evidence_id: str
    kpi: str
    period: str
    definition: str | None
    status: str = Field(description="defined | defined_table | mentioned_no_definition")
    source: str | None = None


class KpiDefinitionPair(BaseModel):
    evidence_id: str
    kpi: str
    change_type: str = Field(description="redefinition | drop")
    prior_period: str
    prior_definition: str
    current_period: str
    current_definition: str
    prefilter_similarity: float
    possible_rename_of: str | None = None


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", text) if s.strip()]


def _matches_kpi(sentence: str, kpi: str) -> bool:
    return any(re.search(p, sentence, re.IGNORECASE) for p in KPI_DICTIONARY[kpi])


def _definition_span(text: str, kpi: str) -> str | None:
    """First definitional sentence for the KPI, extended across continuation
    sentences (multi-sentence definitions)."""
    sents = _sentences(text)
    for i, s in enumerate(sents):
        low = s.lower()
        if any(c in low for c in DEFINITION_CUES) and _matches_kpi(s, kpi):
            span = [s]
            for j in range(i + 1, min(i + MAX_DEF_SENTENCES, len(sents))):
                if any(c in sents[j].lower() for c in CONTINUATION_CUES):
                    span.append(sents[j])
                else:
                    break
            return " ".join(" ".join(x.split()) for x in span)
    return None


def _table_definition(text: str, kpi: str) -> str | None:
    """Fallback: capture reconciliation-table context mentioning the KPI."""
    low = text.lower()
    for cue in RECON_CUES:
        idx = low.find(cue)
        while idx != -1:
            window = text[idx : idx + 400]
            if _matches_kpi(window, kpi):
                return " ".join(window.split())
            idx = low.find(cue, idx + 1)
    return None


def extract_kpi_definitions(documents: list[DocumentRecord]) -> list[KpiDefinition]:
    """One KpiDefinition per (period, KPI) that is at least mentioned."""
    periods = group_documents(documents)
    out: list[KpiDefinition] = []
    counter = 0
    for pd in periods:
        text = pd.all_text
        source = next(iter(pd.sources.values()), None) if pd.sources else None
        for kpi in KPI_DICTIONARY:
            if not any(re.search(p, text, re.IGNORECASE) for p in KPI_DICTIONARY[kpi]):
                continue
            counter += 1
            eid = f"KD-{counter:03d}"
            span = _definition_span(text, kpi)
            if span:
                out.append(KpiDefinition(evidence_id=eid, kpi=kpi, period=pd.fiscal_label,
                                         definition=span[:600], status="defined", source=source))
                continue
            table = _table_definition(text, kpi)
            if table:
                out.append(KpiDefinition(evidence_id=eid, kpi=kpi, period=pd.fiscal_label,
                                         definition=table[:600], status="defined_table", source=source))
                continue
            out.append(KpiDefinition(evidence_id=eid, kpi=kpi, period=pd.fiscal_label,
                                     definition=None, status="mentioned_no_definition", source=source))
    return out


def _latest_prior(defs: list[KpiDefinition], kpi: str, latest_period: str) -> KpiDefinition | None:
    prior = [d for d in defs if d.kpi == kpi and d.period != latest_period and d.definition]
    return prior[-1] if prior else None


def pair_definition_changes(
    documents: list[DocumentRecord],
    defs: list[KpiDefinition] | None = None,
) -> list[KpiDefinitionPair]:
    """Candidate KPI-drift pairs for the latest documented period: redefinitions
    (pre-filtered by token-similarity) and drops (touted then absent)."""
    periods = group_documents(documents)
    if len(periods) < 2:
        return []
    defs = defs if defs is not None else extract_kpi_definitions(documents)
    latest = periods[-1].fiscal_label
    latest_kpis = {d.kpi for d in defs if d.period == latest}
    latest_defined = {d.kpi: d for d in defs if d.period == latest and d.definition}

    pairs: list[KpiDefinitionPair] = []
    counter = 0

    # Redefinitions: latest defined vs most-recent prior defined.
    for kpi, cur in latest_defined.items():
        prior = _latest_prior(defs, kpi, latest)
        if prior is None:
            continue
        sim = _token_similarity(cur.definition or "", prior.definition or "")
        if sim >= PREFILTER_SIMILARITY:
            continue
        counter += 1
        pairs.append(KpiDefinitionPair(
            evidence_id=f"KDP-{counter:03d}", kpi=kpi, change_type="redefinition",
            prior_period=prior.period, prior_definition=prior.definition or "",
            current_period=latest, current_definition=cur.definition or "",
            prefilter_similarity=round(sim, 3),
        ))

    # Drops: KPI defined in a prior period, absent from the latest period entirely.
    prior_defined = {d.kpi: d for d in defs if d.period != latest and d.definition}
    for kpi, prior in prior_defined.items():
        if kpi in latest_kpis:
            continue
        counter += 1
        rename = None
        # Best-effort rename: a newly-introduced KPI whose definition overlaps this one.
        newly = [d for d in latest_defined.values()
                 if d.kpi not in prior_defined and d.definition]
        for cand in newly:
            if _token_similarity(cand.definition or "", prior.definition or "") >= RENAME_OVERLAP:
                rename = cand.kpi
                break
        pairs.append(KpiDefinitionPair(
            evidence_id=f"KDP-{counter:03d}", kpi=kpi, change_type="drop",
            prior_period=prior.period, prior_definition=prior.definition or "",
            current_period=latest, current_definition="",
            prefilter_similarity=0.0, possible_rename_of=rename,
        ))
    return pairs
