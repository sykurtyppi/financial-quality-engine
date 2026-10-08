"""What the mapper selected for a field, as an object rather than a string.

`FieldDiagnostic.tag_used` spells a selection four ways — `us-gaap:Assets`,
`A+B` (an unqualified composite), `LongTermDebtNoncurrent+DebtCurrent`
(debt), None — and the restatement detector parsed it back with a
hand-written parser that assumed every unqualified piece was `us-gaap`, and
chose how to rebuild the figure by the FIELD's name. `SeriesSelection`
carries the components with their taxonomy and the rule that composes them;
`tag_used` is rendered from it, byte-identically to the old strings, so the
two cannot drift while both exist.

Imports only the field registry.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.services.ingestion.fields import Composition, field


class Composer(StrEnum):
    """How a field's value is put together at one date."""

    SINGLE = "single"  # one concept
    STRATEGY = "strategy"  # composition.resolve_by_strategy (SG&A, D&A)
    DEBT = "debt"  # composition.compose_total_debt


def composer_for(field_name: str) -> Composer:
    """The composer the field's registry entry implies."""
    spec = field(field_name)
    if any(s.composition is Composition.DEBT_BREAKDOWN for s in spec.strategies):
        return Composer.DEBT
    if len(spec.strategies) > 1:
        return Composer.STRATEGY
    return Composer.SINGLE


def _bare(component: str) -> str:
    return component.split(":", 1)[-1]


class SeriesSelection(BaseModel):
    """The concepts a field's reported values were built from, qualified and
    in canonical order, and the composer that combines them per date."""

    model_config = ConfigDict(frozen=True)

    field: str
    composer: Composer
    components: tuple[str, ...]

    @classmethod
    def of(cls, field_name: str, components: tuple[str, ...]) -> SeriesSelection:
        return cls(field=field_name, composer=composer_for(field_name), components=components)

    @property
    def tag_used(self) -> str:
        """The legacy selection string, byte-identical to what the mapper
        used to record: a single concept qualified; a strategy field
        qualified when one concept served, else its concepts joined bare;
        debt always joined bare."""
        if self.composer is Composer.SINGLE or (
            self.composer is Composer.STRATEGY and len(self.components) == 1
        ):
            return self.components[0]
        return "+".join(_bare(c) for c in self.components)

    @property
    def concepts(self) -> list[tuple[str, str]]:
        """(taxonomy, concept) pairs, for readers of companyfacts."""
        return [(taxonomy, concept) for taxonomy, _, concept in
                (c.partition(":") for c in self.components)]

    @classmethod
    def from_tag_used(cls, field_name: str, tag_used: str | None) -> SeriesSelection | None:
        """Parse a legacy selection string for a registry field."""
        components = parse_components(tag_used)
        return cls.of(field_name, components) if components else None


def parse_components(tag_used: str | None) -> tuple[str, ...]:
    """The qualified components a legacy selection string names. Unqualified
    pieces are `us-gaap` — every concept the mapper ever composed is — and
    the old `none` placeholder is skipped."""
    components: list[str] = []
    for piece in (tag_used or "").split("+"):
        piece = piece.strip()
        if not piece or piece == "none":
            continue
        taxonomy, sep, concept = piece.partition(":")
        if sep and not (taxonomy and concept):
            continue
        components.append(piece if sep else f"us-gaap:{piece}")
    return tuple(components)
