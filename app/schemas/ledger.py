"""The evidence ledger: every claim a report makes, with the filings behind it.

A report is prose over numbers. The ledger is the same run as data: one
`EvidenceItem` per claim, each naming the filings (accession, form, filed
date, XBRL concept, period) it rests on. Provenance is load-bearing, not
decorative — an item that names no filing and derives from no other item
cannot be constructed, and a claim the builder could not source is listed
under `LedgerDocument.unsourced` with the reason instead of being dropped.

`kind="snapshot"` provenance is a stored companyfacts snapshot (sha256 +
capture date): the source of a between-snapshot revision, which by
definition has no filing announcing it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.financials import Sign

LEDGER_SCHEMA_VERSION = "1"


class Plane(StrEnum):
    ACCOUNTING = "accounting"
    CAPITAL_MARKETS = "capital_markets"
    FILING_BEHAVIOR = "filing_behavior"
    NARRATIVE = "narrative"
    CONSISTENCY = "consistency"


class ValidationStatus(StrEnum):
    """The decision card's tiers: 1 validated, 2 directional, 3 unvalidated."""

    VALIDATED = "validated"
    DIRECTIONAL = "directional"
    UNVALIDATED = "unvalidated"


class Provenance(BaseModel):
    """One source of an item: a filed fact or document, or a stored snapshot."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["filing", "snapshot"] = "filing"
    accession: str | None = None
    form: str | None = None
    filed: date | None = None
    concept: str | None = None  # qualified XBRL concept, for a fact
    period_start: date | None = None
    period_end: date | None = None
    value: float | None = None
    sign: Sign = 1  # the fact's sign in the value it feeds: added (+1) or subtracted (-1)
    method: str | None = None  # how the mapper used it: direct, ytd_diff, …
    # what this source is to the item: the metric input it feeds
    # ("revenue_prior"), or original / current / amendment, older / newer snapshot
    role: str | None = None
    url: str | None = None
    excerpt: str | None = None
    snapshot_sha256: str | None = None
    captured: date | None = None

    @model_validator(mode="after")
    def _complete(self) -> Provenance:
        if self.kind == "filing":
            if not (self.accession and self.form and self.filed):
                raise ValueError(
                    "filing provenance needs accession, form and filed "
                    f"(got {self.accession!r}, {self.form!r}, {self.filed!r})"
                )
        elif not (self.snapshot_sha256 and self.captured):
            raise ValueError("snapshot provenance needs snapshot_sha256 and captured")
        return self


class EvidenceItem(BaseModel):
    """One claim. It rests on filings (`provenance`) or on other items of the
    same ledger (`derived_from`) — never on nothing."""

    model_config = ConfigDict(frozen=True)

    id: str
    plane: Plane
    kind: str  # metric, narrative_evidence, mismatch, offering, restatement_footprint, …
    subject: str  # the metric, field, detector or form the claim is about
    claim: str
    fiscal_label: str | None = None
    value: float | None = None
    formula: str | None = None
    inputs: dict[str, float | None] = Field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()
    derived_from: tuple[str, ...] = ()
    change_state: str | None = None  # revised, withdrawn, recomposed, amended, …
    validation_status: ValidationStatus
    note: str | None = None

    @model_validator(mode="after")
    def _sourced(self) -> EvidenceItem:
        if not self.provenance and not self.derived_from:
            raise ValueError(f"{self.id} ({self.kind} {self.subject}) names no source")
        return self

    def accessions(self) -> list[str]:
        """Distinct filings behind this item, in order."""
        return list(dict.fromkeys(p.accession for p in self.provenance if p.accession))


class Unsourced(BaseModel):
    """A claim the report makes that the ledger could not source."""

    model_config = ConfigDict(frozen=True)

    plane: Plane
    kind: str
    subject: str
    claim: str
    reason: str


class LedgerDocument(BaseModel):
    """The ledger of one report run."""

    schema_version: str = LEDGER_SCHEMA_VERSION
    ticker: str
    generated_on: date
    fetched_at: str | None = None
    fresh: bool = False
    config_version: str
    coverage: float | None = None
    # field -> the concept(s) its values were read from (the mapper's selection)
    selections: dict[str, str] = Field(default_factory=dict)
    # stream -> "checked" | "not run" | "data failure: …" | "internal error: …"
    streams: dict[str, str] = Field(default_factory=dict)
    items: list[EvidenceItem] = Field(default_factory=list)
    unsourced: list[Unsourced] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> LedgerDocument:
        ids = [i.id for i in self.items]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate evidence ids: {dupes}")
        known = set(ids)
        for item in self.items:
            missing = [d for d in item.derived_from if d not in known]
            if missing:
                raise ValueError(f"{item.id} derives from unknown items {missing}")
        return self

    def item(self, item_id: str) -> EvidenceItem:
        return next(i for i in self.items if i.id == item_id)
