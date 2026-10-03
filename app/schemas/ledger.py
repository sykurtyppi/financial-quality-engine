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

`kind="observation"` provenance is the operator's market observation (the
valuation shadow card's price: its timestamp, its source as the operator
wrote it, when it was recorded, and the sha256 of the file it was read
from). It names no filing and is not reconcilable to one; the console says
so instead of offering a tick (Hermes review of 02c2aac, valuation plane).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.financials import Sign

LEDGER_SCHEMA_VERSION = "1"


class Plane(StrEnum):
    ACCOUNTING = "accounting"
    CAPITAL_MARKETS = "capital_markets"
    FILING_BEHAVIOR = "filing_behavior"
    NARRATIVE = "narrative"
    CONSISTENCY = "consistency"
    # The valuation shadow card: unscored, so every item is UNVALIDATED.
    VALUATION = "valuation"


class ValidationStatus(StrEnum):
    """The decision card's tiers: 1 validated, 2 directional, 3 unvalidated."""

    VALIDATED = "validated"
    DIRECTIONAL = "directional"
    UNVALIDATED = "unvalidated"


# The fields of each kind. A provenance carries its own kind's fields and
# none of another's: a "filing" with an observation's timestamp, or an
# "observation" with an accession, would be a row the console renders as
# one thing while it claims another (review of 48b1f04, F6).
_OBSERVATION_FIELDS = ("observed_at", "source", "recorded_at", "observation_sha256")
_FILED_FIELDS = ("accession", "form", "filed", "concept", "period_start", "period_end",
                 "snapshot_sha256", "captured")


class Provenance(BaseModel):
    """One source of an item: a filed fact or document, a stored snapshot,
    or the operator's market observation."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["filing", "snapshot", "observation"] = "filing"
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
    # An observation: when the price was seen, what the operator looked at,
    # when it was recorded, and the digest of the observation file's bytes.
    # The times are aware: a naive one has no moment (F6).
    observed_at: AwareDatetime | None = None
    source: str | None = None
    recorded_at: AwareDatetime | None = None
    observation_sha256: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> Provenance:
        foreign = _FILED_FIELDS if self.kind == "observation" else _OBSERVATION_FIELDS
        carried = [name for name in foreign if getattr(self, name) is not None]
        if carried:
            raise ValueError(f"{self.kind} provenance does not carry {', '.join(carried)}")
        if self.kind == "filing":
            if not (self.accession and self.form and self.filed):
                raise ValueError(
                    "filing provenance needs accession, form and filed "
                    f"(got {self.accession!r}, {self.form!r}, {self.filed!r})"
                )
        elif self.kind == "snapshot":
            if not (self.snapshot_sha256 and self.captured):
                raise ValueError("snapshot provenance needs snapshot_sha256 and captured")
        else:
            missing = [name for name in _OBSERVATION_FIELDS if not getattr(self, name)]
            if missing:
                raise ValueError(f"observation provenance needs {', '.join(missing)}")
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


class ValuationSummary(BaseModel):
    """What the valuation shadow card did this run: `state` is "produced" or
    "not produced: no market observation" — a plane that cannot be computed
    fails the build closed, and no ledger is written at all. The rest is
    set only when produced: the observation's provenance, the period the
    bridge rests on and how its availability at the observation was
    decided (`availability`, the card's own sentence), and the EV or the
    reason none was asserted."""

    model_config = ConfigDict(frozen=True)

    state: str
    observation: Provenance | None = None
    fiscal_label: str | None = None
    availability: str | None = None
    ev: float | None = None
    ev_reason: str | None = None


class LedgerDocument(BaseModel):
    """The ledger of one report run."""

    schema_version: str = LEDGER_SCHEMA_VERSION
    # The run this ledger belongs to, stamped when it is published
    # (`report_files.replacing`): the report beside it names the same id on
    # its last line. None for a ledger never published (the API, tests).
    generation_id: str | None = None
    # The engine commit that built the run (`report_files.engine_commit`),
    # stamped at the same time; the report states it on the line above.
    engine_commit: str | None = None
    ticker: str
    # The company's CIK, which a filing's folder on EDGAR is filed under
    # (`/Archives/edgar/data/<cik>/<accession without dashes>/`), so a
    # reviewer can open each accession. Recorded from the run's resolved
    # CIK as its payloads carry it (`reporting.ledger.ledger_cik`); withheld
    # when any payload names a different one or something that is not a
    # CIK, and `cik_note` then says what each gave. None with no note when
    # the run held no source (the API, tests), or in a ledger written before
    # the field existed. Strict: only an int in (0, 10**10) loads — the
    # console links it (review of e37827a, finding N1).
    cik: Annotated[int, Field(strict=True, gt=0, lt=10**10)] | None = None
    cik_note: str | None = None
    generated_on: date
    fetched_at: str | None = None
    fresh: bool = False
    config_version: str
    coverage: float | None = None
    # field -> the concept(s) its values were read from (the mapper's
    # selection, `SeriesSelection.label`): `tag_used`, then
    # "|<period end>:<concept>" for each quarter filled from another concept
    # after a tag switch. A field without such quarters is just `tag_used`.
    selections: dict[str, str] = Field(default_factory=dict)
    # stream -> "checked" | "checked (incomplete: <the scan's coverage>)" (the
    # restatement scan) | "not compared: …" (vintage) | "not run" |
    # "data failure: …" | "internal error: …"
    streams: dict[str, str] = Field(default_factory=dict)
    items: list[EvidenceItem] = Field(default_factory=list)
    unsourced: list[Unsourced] = Field(default_factory=list)
    # The valuation shadow card's state this run; None when the caller did
    # not ask for the plane (the API, a replay) and in every ledger written
    # before it existed. Its rows are `items` on `Plane.VALUATION`.
    valuation: ValuationSummary | None = None

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
