"""Point-in-time dataset construction.

companyfacts entries carry the `filed` date of the filing that reported them,
so a true point-in-time view exists for free: keep only facts filed on or
before the as-of date, then run the ordinary mapper. Amendment dedupe
(latest-filed wins) then operates only on what was knowable at the time.

Facts without a `filed` date are DROPPED in PIT mode (they cannot be dated,
and including them would leak future information).
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import CompanyDataset
from app.services.ingestion import companyfacts_mapper as m
from app.services.ingestion.companyfacts_mapper import IngestionDiagnostics, build_dataset


def mapped_tags() -> set[tuple[str, str]]:
    tags: set[tuple[str, str]] = set()
    for cands in list(m.INSTANT_FIELDS.values()) + list(m.FLOW_FIELDS.values()):
        tags.update(cands)
    tags.update(m.SGA_COMPONENTS)
    tags.update(m.DA_COMPONENTS)
    # Debt + finance-lease tags the mapper composes into total_debt (review
    # finding 2: omitting the finance-lease tags made PIT debt diverge from live).
    debt_tags = (
        m.DEBT_NONCURRENT
        + m.DEBT_CURRENT
        + m.DEBT_TOTAL
        + m.DEBT_SHORT
        + m.FINANCE_LEASE_NONCURRENT
        + m.FINANCE_LEASE_CURRENT
    )
    for tag in debt_tags:
        tags.add(("us-gaap", tag))
    return tags


def trim_to_mapped_tags(facts_json: dict) -> dict:
    """Drop concepts the mapper never reads (perf: PIT filtering runs per
    as-of date, so the input should be small)."""
    keep = mapped_tags()
    out = {"entityName": facts_json.get("entityName"), "facts": {}}
    for taxonomy, tag in keep:
        concept = facts_json.get("facts", {}).get(taxonomy, {}).get(tag)
        if concept:
            out["facts"].setdefault(taxonomy, {})[tag] = concept
    return out


def filter_as_of(facts_json: dict, as_of: date) -> dict:
    """Keep only fact entries filed on or before `as_of`."""
    cutoff = as_of.isoformat()
    out = {"entityName": facts_json.get("entityName"), "facts": {}}
    for taxonomy, tags in facts_json.get("facts", {}).items():
        for tag, concept in tags.items():
            units_out = {}
            for unit, entries in concept.get("units", {}).items():
                kept = [e for e in entries if e.get("filed") and e["filed"] <= cutoff]
                if kept:
                    units_out[unit] = kept
            if units_out:
                out["facts"].setdefault(taxonomy, {})[tag] = {"units": units_out}
    return out


def build_pit_dataset(
    trimmed_facts: dict,
    ticker: str,
    as_of: date,
    n_quarters: int = 8,
    sector: str | None = None,
) -> tuple[CompanyDataset, IngestionDiagnostics]:
    pit = filter_as_of(trimmed_facts, as_of)
    return build_dataset(pit, ticker=ticker, n_quarters=n_quarters, sector=sector)
