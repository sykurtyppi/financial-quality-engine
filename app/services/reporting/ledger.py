"""Build the evidence ledger of one report run.

Every claim the report makes becomes an `EvidenceItem` naming the filings
behind it. The builder reads what the run already produced — the analysis
result, the mapped dataset (whose `PeriodFinancials.sources` carry each
value's filed facts), and the evidence streams — and adds nothing to the
report itself. A claim it cannot source is listed in `unsourced` with the
reason; it is never dropped and never given a guessed source.

Item ids are content-derived (`EV-` + a digest of what the item is about),
so the same claim keeps its id across runs and a journal entry can cite it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from app.config import scoring_config as cfg
from app.schemas.financials import CompanyDataset, DocumentRecord, SourcedValue
from app.schemas.ledger import (
    EvidenceItem,
    LedgerDocument,
    Plane,
    Provenance,
    Unsourced,
    ValidationStatus,
)
from app.schemas.metrics import MetricResult
from app.schemas.report import AnalysisResult, EvidenceEntry, NarrativeEvidence
from app.services.formulas.registry import MetricsBundle, compute_metrics
from app.services.metrics_registry import FINANCIAL_METRICS, NARRATIVE_METRICS
from app.services.provenance import sources_for
from app.services.reporting.decision_card import tier_of

_STATUS = {
    1: ValidationStatus.VALIDATED,
    2: ValidationStatus.DIRECTIONAL,
    3: ValidationStatus.UNVALIDATED,
}
# "derived from FY2025Q1 documents: " / "compared with FY2024Q1 documents: "
_DERIVED_PREFIX = re.compile(r"^(?:derived from |compared with )?\S+ documents(?:: | \(.*\))?")


def _status(names: Iterable[str]) -> ValidationStatus:
    return _STATUS[tier_of(names)]


def _id(*parts: object) -> str:
    key = "\x1f".join("" if p is None else str(p) for p in parts)
    return "EV-" + hashlib.sha256(key.encode()).hexdigest()[:10]


def _filing(
    accession: str | None,
    form: str | None,
    filed: date | None,
    **kw: Any,
) -> Provenance | None:
    """A filing source, or None when the filing is not fully identified —
    an incomplete source is reported as such, never padded out."""
    if not (accession and form and filed):
        return None
    return Provenance(kind="filing", accession=accession, form=form, filed=filed, **kw)


def _document(doc: DocumentRecord, **kw: Any) -> Provenance | None:
    return _filing(doc.accession, doc.form, doc.filed, **kw)


def _edgar_url(cik: int | None, accession: str, document: str = "") -> str | None:
    if not cik:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"


class _Builder:
    def __init__(self) -> None:
        self.items: list[EvidenceItem] = []
        self.unsourced: list[Unsourced] = []
        self._ids: set[str] = set()

    def add(self, item_id: str, **kw: Any) -> str | None:
        """Add an item, or record why it could not be sourced. Returns its id
        when added."""
        if item_id in self._ids:
            return item_id
        if not kw.get("provenance") and not kw.get("derived_from"):
            self.unsourced.append(Unsourced(
                plane=kw["plane"], kind=kw["kind"], subject=kw["subject"], claim=kw["claim"],
                reason=kw.pop("why_unsourced", "no source identified"),
            ))
            return None
        kw.pop("why_unsourced", None)
        self.items.append(EvidenceItem(id=item_id, **kw))
        self._ids.add(item_id)
        return item_id


# --- accounting metrics -----------------------------------------------------


def _fact_sources(found: dict[str, list[SourcedValue]]) -> tuple[list[Provenance], int]:
    """(provenance, facts that name no filing) for a metric's resolved inputs."""
    out: list[Provenance] = []
    incomplete = 0
    for role, values in found.items():
        for sv in values:
            for ref in sv.inputs:
                p = _filing(
                    ref.accession, ref.form, ref.filed, concept=ref.concept,
                    period_start=ref.start, period_end=ref.end, value=ref.value,
                    sign=ref.sign, method=sv.method, role=role,
                )
                if p is None:
                    incomplete += 1
                else:
                    out.append(p)
    return out, incomplete


def _history(bundle: MetricsBundle, name: str, label: str) -> MetricResult | None:
    return next((m for m in bundle.history.get(name, []) if m.fiscal_label == label), None)


def _metric_items(
    b: _Builder, entries: list[EvidenceEntry], dataset: CompanyDataset
) -> dict[str, str]:
    """One item per metric the report evidences. Returns name -> item id."""
    bundle = compute_metrics(dataset)
    ids: dict[str, str] = {}
    for e in entries:
        common: dict[str, Any] = dict(
            kind="metric", subject=e.metric_name, claim=e.claim, fiscal_label=e.fiscal_label,
            value=e.value, formula=e.formula, inputs=e.inputs,
            validation_status=_status({e.metric_name}),
        )
        item_id = _id("metric", e.metric_name, e.fiscal_label)
        if e.metric_name in NARRATIVE_METRICS:
            docs = [d for d in dataset.documents if d.fiscal_label == e.fiscal_label]
            prov = [p for d in docs if (p := _document(d, role="period document")) is not None]
            added = b.add(
                item_id, plane=Plane.NARRATIVE, provenance=tuple(prov), **common,
                why_unsourced=f"no {e.fiscal_label} document names its filing",
            )
        elif e.metric_name in FINANCIAL_METRICS:
            metric = _history(bundle, e.metric_name, e.fiscal_label)
            found = sources_for(dataset, metric, bundle=bundle) if metric is not None else {}
            prov, incomplete = _fact_sources(found)
            note = None
            if incomplete:
                note = f"{incomplete} input fact(s) name no filing and are not listed"
            if found and "[" in next(iter(found)):
                note = (note + "; " if note else "") + (
                    "a statistic over its base metric's history: sources are that "
                    "metric's in each period it may read"
                )
            added = b.add(
                item_id, plane=Plane.ACCOUNTING, provenance=tuple(prov), note=note, **common,
                why_unsourced="its inputs carry no per-value provenance "
                "(dataset not mapped from companyfacts in this run)",
            )
        else:
            added = b.add(
                item_id, plane=Plane.ACCOUNTING, **common,
                why_unsourced="not a registered metric",
            )
        if added is not None:
            ids[e.metric_name] = added
    return ids


# --- narrative --------------------------------------------------------------


def _cited(source: str, documents: list[DocumentRecord]) -> list[DocumentRecord]:
    """The documents a narrative row's `source` names, in document order."""
    pieces = set()
    for part in source.split("; "):
        part = _DERIVED_PREFIX.sub("", part).strip()
        if part:
            pieces.add(part)
    seen: set[str] = set()
    out = []
    for d in documents:
        if d.source and d.source in pieces and d.source not in seen:
            seen.add(d.source)
            out.append(d)
    return out


def _narrative_items(
    b: _Builder, rows: list[NarrativeEvidence], documents: list[DocumentRecord]
) -> dict[str, str]:
    """One item per narrative evidence row. Returns NE id -> item id."""
    ids: dict[str, str] = {}
    for row in rows:
        cited = _cited(row.source, documents)
        note = None
        quoted = not row.source.startswith("derived from")
        if not cited:
            # Not located in one document, or no source recorded: the
            # period's documents are where it came from — say so.
            cited = [d for d in documents if d.fiscal_label == row.fiscal_label]
            note = f"source recorded as {row.source!r}; provenance lists the period's documents"
            quoted = False
        prov = [
            p for d in cited
            if (p := _document(d, role=d.doc_type.value, excerpt=row.excerpt if quoted else None))
            is not None
        ]
        item_id = _id("narrative_evidence", row.detector, row.fiscal_label, row.comparison,
                      row.excerpt)
        added = b.add(
            item_id, plane=Plane.NARRATIVE, kind="narrative_evidence", subject=row.detector,
            claim=row.detail, fiscal_label=row.fiscal_label, provenance=tuple(prov),
            validation_status=_status({row.detector}), note=note,
            why_unsourced=f"source recorded as {row.source!r} and no document names its filing",
        )
        if added is not None:
            ids[row.evidence_id] = added
    return ids


def _mismatch_items(
    b: _Builder, result: AnalysisResult, ne_ids: dict[str, str], metric_ids: dict[str, str]
) -> None:
    for m in result.mismatches:
        derived = [ne_ids[m.narrative_evidence_id]] if m.narrative_evidence_id in ne_ids else []
        derived += [metric_ids[n] for n in m.metric_names if n in metric_ids]
        b.add(
            _id("mismatch", m.kind, m.fiscal_label, m.narrative_evidence_id),
            plane=Plane.CONSISTENCY, kind="mismatch", subject=m.kind, claim=m.detail,
            fiscal_label=m.fiscal_label, inputs=dict(m.metric_values),
            derived_from=tuple(dict.fromkeys(derived)),
            validation_status=_status(m.metric_names),
            why_unsourced="neither its narrative row nor its metrics are in the ledger",
        )


# --- evidence streams ---------------------------------------------------------


def _offering_items(b: _Builder, timeline: Any) -> None:
    cik = getattr(timeline, "cik", None)
    for f in timeline.filings:
        detail = f.kind + (f", {f.security_type}" if f.kind == "takedown" else "")
        p = _filing(
            f.accession, f.form, f.filing_date, role=f.kind,
            url=_edgar_url(cik, f.accession, f.primary_doc), excerpt=f.excerpt or None,
        )
        b.add(
            _id("offering", f.accession, f.form),
            plane=Plane.CAPITAL_MARKETS, kind="offering", subject=f.form,
            claim=f"{f.form} filed {f.filing_date} ({detail})",
            provenance=(p,) if p else (), validation_status=ValidationStatus.DIRECTIONAL,
            why_unsourced="the filing is not fully identified",
        )


def _footprint_items(b: _Builder, footprints: list[Any]) -> None:
    for fp in footprints:
        common = dict(concept=fp.tag, period_start=fp.period_start, period_end=fp.period_end)
        roles = [
            ("original", fp.original_accession, fp.original_form, fp.original_filed,
             fp.original_value),
            ("current", fp.current_accession, fp.current_form, fp.current_filed,
             fp.current_value),
            ("amendment", fp.amendment_accession, fp.amendment_form, fp.amendment_filed,
             fp.amendment_value),
        ]
        prov = [
            p for role, accession, form, filed, value in roles
            if (p := _filing(accession, form, filed, role=role, value=value, **common))
            is not None
        ]
        period = f"{fp.period_start} → {fp.period_end}" if fp.period_start else str(fp.period_end)
        b.add(
            _id("restatement_footprint", fp.field_name, fp.tag, fp.period_start, fp.period_end),
            plane=Plane.ACCOUNTING, kind="restatement_footprint", subject=fp.field_name,
            claim=(
                f"{fp.field_name} for {period} as originally reported "
                f"{fp.original_value:,.0f} ({fp.original_form} filed {fp.original_filed}), "
                f"now {fp.current_value:,.0f} ({fp.current_form} filed {fp.current_filed})"
            ),
            value=fp.current_value, provenance=tuple(prov),
            change_state="amended" if fp.is_amendment else "revised",
            validation_status=(
                ValidationStatus.VALIDATED if fp.is_amendment else ValidationStatus.DIRECTIONAL
            ),
            why_unsourced="none of its filings is fully identified",
        )


def _derived_items(b: _Builder, derived: Iterable[Any]) -> None:
    """A derived quarter that moved between the filings behind it
    (`restatements.derived_revisions`). Its sources are the filings made on
    the day it last moved; the original figure is named in the claim by its
    filing date — it was rebuilt from everything filed by then, not read
    from one filing."""
    for dr in derived:
        common = dict(period_start=dr.period_start, period_end=dr.period_end,
                      value=dr.current_value, method=dr.method, role="moved the derived value")
        prov = [
            p for form, accession in dr.moved_by
            if (p := _filing(accession, form, dr.current_filed, **common)) is not None
        ]
        period = f"{dr.period_start} → {dr.period_end}" if dr.period_start else str(dr.period_end)
        b.add(
            _id("derived_revision", dr.field_name, dr.period_start, dr.period_end),
            plane=Plane.ACCOUNTING, kind="derived_revision", subject=dr.field_name,
            claim=(
                f"derived {dr.field_name} for {period} ({dr.method}) rebuilt from filings "
                f"through {dr.original_filed}: {dr.original_value:,.0f}; from filings through "
                f"{dr.current_filed}: {dr.current_value:,.0f}"
            ),
            value=dr.current_value, provenance=tuple(prov),
            change_state="amended" if dr.is_amendment else "revised",
            validation_status=(
                ValidationStatus.VALIDATED if dr.is_amendment else ValidationStatus.DIRECTIONAL
            ),
            why_unsourced="no filing made on the day it moved is fully identified",
        )


def _non_reliance_items(b: _Builder, events: Any, since: date, report_date: date) -> None:
    for filed, accession, form in events.non_reliance_8k_filings:
        if not since <= filed <= report_date:
            continue
        p = _filing(accession, form, filed, role="8-K Item 4.02")
        b.add(
            _id("non_reliance_8k_402", accession),
            plane=Plane.FILING_BEHAVIOR, kind="non_reliance_8k_402", subject=form,
            claim=f"8-K Item 4.02 non-reliance (restatement announced) filed {filed}",
            provenance=(p,) if p else (), change_state="announced",
            validation_status=_status({"non_reliance_8k_402"}),
            why_unsourced="the 8-K is not fully identified",
        )


# Ledger kinds for filing events: the card's signal name where the event has
# one (so a 4.02 keeps the id it had before this stream existed).
_EVENT_KINDS = {
    "non_reliance": "non_reliance_8k_402",
    "auditor_change": "auditor_change_8k_401",
    "late_filing_notice": "missed_deadline_nt",
    "impairment": "impairment_8k_206",
    "amendment": "periodic_report_amendment",
    "filing_lag": "filing_lag_drift",
}


def _filing_event_items(b: _Builder, fe: Any) -> None:
    """Every dated filing event, each resting on the filing itself."""
    for e in fe.events:
        kind = _EVENT_KINDS[e.kind]
        p = _filing(e.accession, e.form, e.filed, role=e.kind)
        b.add(
            _id(kind, e.accession),
            plane=Plane.FILING_BEHAVIOR, kind=kind, subject=e.form, claim=e.detail,
            provenance=(p,) if p else (),
            change_state="announced" if e.signal else None,
            validation_status=(
                _status({e.signal}) if e.signal else ValidationStatus.DIRECTIONAL
            ),
            why_unsourced="the filing is not fully identified",
        )


def _snapshot(obs: Any, role: str) -> Provenance:
    return Provenance(
        kind="snapshot", snapshot_sha256=obs.sha256,
        captured=date.fromisoformat(obs.captured), role=role,
    )


def _vintage_items(b: _Builder, rep: Any, floor: date) -> None:
    from app.services.ingestion.vintages import silent_revision_tier1_lines

    windows = []
    if rep.compared and rep.previous is not None and rep.newest is not None:
        windows.append((rep.changes_since_previous, rep.previous, rep.newest))
    if rep.changes_since_baseline is not None and rep.baseline is not None:
        windows.append((rep.changes_since_baseline, rep.baseline, rep.newest))
    for changes, older, newer in windows:
        for c in changes:
            prov = [_snapshot(older, "older snapshot"), _snapshot(newer, "newer snapshot")]
            concept = f"{c.key.taxonomy}:{c.key.tag}"
            for role, accession, form, filed, value in (
                ("as before", c.old_accession, c.old_form, c.old_filed, c.old_value),
                ("as now", c.new_accession, c.new_form, c.new_filed, c.new_value),
            ):
                p = _filing(accession, form, filed, role=role, value=value, concept=concept,
                            period_start=c.key.start, period_end=c.key.end)
                if p is not None:
                    prov.append(p)
            promoted = bool(silent_revision_tier1_lines([c], older.captured, newer.captured,
                                                        period_since=floor))
            if c.kind == "withdrawn":
                what = f"withdrawn (was {c.old_value:,.0f})"
            else:
                what = f"{c.old_value:,.0f} → {c.new_value:,.0f}"
            b.add(
                _id("silent_revision", older.sha256, newer.sha256, c.field_name, concept,
                    c.key.start, c.key.end),
                plane=Plane.ACCOUNTING, kind="silent_revision", subject=c.field_name,
                claim=(f"{c.field_name} for {c.key.period}: {what} between snapshots "
                       f"{older.captured} and {newer.captured}"),
                value=c.new_value, provenance=tuple(prov),
                change_state="recomposed" if c.moved_tag else c.kind,
                validation_status=(
                    ValidationStatus.VALIDATED if promoted else ValidationStatus.DIRECTIONAL
                ),
                note="; ".join(n for n in (
                    "before the scored window (context)" if c.scope == "context" else "",
                    f"moved with {c.new_form} {c.new_accession}, which the newer snapshot "
                    "carries beside the original: not silent (the restatement scan reports it)"
                    if c.explained_by_filing else "",
                ) if n) or None,
            )


# --- the document -------------------------------------------------------------


def _stream_state(name: str, ran: bool, errors: dict[str, Any], rep: Any = None) -> str:
    if not ran:
        return "not run"
    failure = errors.get(name)
    if failure is not None:
        kind = "internal error" if getattr(failure, "internal", False) else "data failure"
        return f"{kind}: {failure}"
    if name == "vintage" and rep is not None and not rep.compared:
        return f"not compared: {rep.no_baseline_reason}"
    return "checked"


def build_ledger(
    *,
    result: AnalysisResult,
    dataset: CompanyDataset,
    ticker: str,
    report_date: date,
    fetched_at: str | None = None,
    fresh: bool = False,
    coverage: float | None = None,
    field_tags: Any = None,
    streams: dict[str, Any] | None = None,
    errors: dict[str, Any] | None = None,
) -> LedgerDocument:
    """The ledger of one run. `streams` holds the raw stream objects
    (`offerings`, `restatements`, `events`, `filing_events`, `vintage`) when a client ran
    them; `errors` the per-stream failures, as the report renders them."""
    b = _Builder()
    metric_ids = _metric_items(b, result.evidence, dataset)
    ne_ids = _narrative_items(b, result.narrative_evidence, dataset.documents)
    _mismatch_items(b, result, ne_ids, metric_ids)

    streams = streams or {}
    errors = errors or {}
    ran = bool(streams.get("ran"))
    if (timeline := streams.get("offerings")) is not None and errors.get("offerings") is None:
        _offering_items(b, timeline)
    if (scan := streams.get("restatements")) is not None:
        _footprint_items(b, scan.footprints)
        _derived_items(b, scan.derived)
    floor = date(report_date.year - 2, report_date.month, min(report_date.day, 28))
    if (behaviour := streams.get("filing_events")) is not None:
        _filing_event_items(b, behaviour)
    elif (events := streams.get("events")) is not None:
        _non_reliance_items(b, events, floor, report_date)
    rep = streams.get("vintage")
    if rep is not None:
        _vintage_items(b, rep, floor)

    selections: dict[str, str] = {}
    for name, sel in (field_tags or {}).items():
        tag = getattr(sel, "tag_used", sel)
        if tag:
            selections[name] = str(tag)

    return LedgerDocument(
        ticker=ticker.upper(),
        generated_on=report_date,
        fetched_at=fetched_at,
        fresh=fresh,
        config_version=cfg.CONFIG_VERSION,
        coverage=coverage,
        selections=selections,
        streams={
            name: _stream_state(name, ran, errors, rep)
            for name in ("offerings", "restatements", "events", "filing_events", "vintage")
        },
        items=b.items,
        unsourced=b.unsourced,
    )
