"""A zero or negative share count is refused where it is used, not by the
schema (review of #119 / 22fa77f).

#119 made `PeriodFinancials.shares_diluted` / `shares_outstanding` `gt=0`.
The companyfacts mapper drops only non-finite values (`_collect`), so ONE
filed share fact of 0 or below in the window made `build_dataset` raise a
pydantic ValidationError, which is a ValueError: the live report failed
outright, and the restatement scan, the vintage diff and the valuation
plane's as-filed rebuild, which catch ValueError, silently took the firm
as unmappable. r17 decided to refuse a bad value where it is used for
exactly this reason; the schema is a carrier. The users refuse it: the
share-change formulas' "Non-positive prior share count" guard and the
valuation bridge's INVALID_SHARE_COUNT (with the expectations' reason).

Each test here maps a real fixture payload with one share fact set to 0 or
to a negative value.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import find, settings

from app.schemas.ledger import LedgerDocument
from app.schemas.metrics import MetricStatus
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import scan_restatements
from app.services.ingestion.vintages import diff_scored
from app.services.journal import reporting as journal_reporting
from app.services.reporting.report_builder import ledger_path
from app.services.valuation.bridge import INVALID_SHARE_COUNT
from app.services.valuation.observation import (
    LoadedObservation,
    MarketObservation,
    Scenario,
    write_observation,
)
from app.services.valuation.plane import compute_plane
from tests.integration.test_ledger_provenance import _Client, _submissions
from tests.strategies import period_datasets

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
BAD = [0.0, -1.0]

# KO's FY2025Q2 diluted weighted average: its one quarter-length fact. The
# quarters either side carry their own, so the next quarter's share change
# reads it as its prior.
MID = ("WeightedAverageNumberOfDilutedSharesOutstanding", "2025-03-29", "2025-06-27")
# FY2025Q2's cover-page count (dated after the quarter, on the 10-Q's cover).
MID_COVER = "2025-07-22"
# KO's newest quarter, FY2026Q1: its diluted fact and its cover-page count.
# The bridge reads the cover-page count first and the diluted one without it.
NEWEST_DILUTED = ("WeightedAverageNumberOfDilutedSharesOutstanding", "2026-01-01", "2026-04-03")
NEWEST_COVER = "2026-04-28"


def _ko() -> dict:
    return json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())


def _set_diluted(facts: dict, fact: tuple[str, str, str], value: float) -> dict:
    """`facts` with the one diluted-share fact `fact` reporting `value`."""
    out = copy.deepcopy(facts)
    tag, start, end = fact
    rows = [r for r in out["facts"]["us-gaap"][tag]["units"]["shares"]
            if r.get("start") == start and r["end"] == end]
    assert len(rows) == 1, rows
    rows[0]["val"] = value
    return out


def _cover(facts: dict) -> list[dict]:
    return facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]


def _set_cover(facts: dict, value: float | None, end: str = NEWEST_COVER) -> dict:
    """`facts` with the cover-page count dated `end` at `value`, or without
    it (None)."""
    out = copy.deepcopy(facts)
    rows = _cover(out)
    (row,) = [r for r in rows if r["end"] == end]
    if value is None:
        rows.remove(row)
    else:
        row["val"] = value
    return out


def _bridge_payload(field: str, value: float) -> dict:
    """KO with the share count the bridge reads for FY2026Q1 at `value`."""
    if field == "shares_outstanding":
        return _set_cover(_ko(), value)
    return _set_cover(_set_diluted(_ko(), NEWEST_DILUTED, value), None)


def _obs(price: float = 61.0) -> MarketObservation:
    now = datetime.now(UTC)
    return MarketObservation(
        ticker="KO", price=price, currency="USD", observed_at=now - timedelta(hours=1),
        source="test: synthetic observation", recorded_at=now,
        scenarios=(Scenario(name="base", fcf_growth=0.05, years=5),),
    )


# --- the mapper builds it; the share-change formula refuses it ------------------


@pytest.mark.parametrize("value", BAD)
def test_one_bad_diluted_share_fact_maps_and_the_share_change_after_it_is_not_meaningful(value):
    """Failed on e03b470: `build_dataset` raised (`shares_diluted` must be
    greater than 0)."""
    ds, _ = build_dataset(_set_diluted(_ko(), MID, value), "KO")
    by_label = {p.fiscal_label: p for p in ds.periods}
    assert by_label["FY2025Q2"].shares_diluted == value
    assert by_label["FY2025Q1"].shares_diluted == 4_313_000_000

    history = {m.fiscal_label: m for m in compute_metrics(ds).history["diluted_share_growth"]}
    after = history["FY2025Q3"]
    assert after.status is MetricStatus.NOT_MEANINGFUL and after.value is None
    assert after.note is not None and "Non-positive prior share count" in after.note
    # What the engine does with the bad quarter as the CURRENT count: the
    # formula guards only the prior, so the change reads as a -100% (or
    # beyond) OK value. Pinned as found, not endorsed: the formula is
    # flag-only (AGENTS.md §4), and the finding is reported for review.
    own = history["FY2025Q2"]
    assert own.status is MetricStatus.OK
    assert own.value == pytest.approx((value - 4_313_000_000) / 4_313_000_000)


@pytest.mark.parametrize("value", BAD)
def test_one_bad_cover_page_count_maps_and_the_net_share_change_after_it_is_not_meaningful(value):
    """Failed on e03b470: `shares_outstanding` must be greater than 0."""
    ds, _ = build_dataset(_set_cover(_ko(), value, MID_COVER), "KO")
    by_label = {p.fiscal_label: p for p in ds.periods}
    assert by_label["FY2025Q2"].shares_outstanding == value

    history = {m.fiscal_label: m for m in compute_metrics(ds).history["net_share_count_change"]}
    after = history["FY2025Q3"]
    assert after.status is MetricStatus.NOT_MEANINGFUL and after.value is None
    assert after.note is not None and "Non-positive prior share count" in after.note
    # As for the diluted count: the bad quarter as the current count reads
    # as an OK change of -100% or beyond (pinned as found, not endorsed).
    own = history["FY2025Q2"]
    assert own.status is MetricStatus.OK
    assert own.value == pytest.approx((value - 4_304_266_738) / 4_304_266_738)


# --- the valuation bridge refuses it ---------------------------------------------


@pytest.mark.parametrize("field", ["shares_outstanding", "shares_diluted"])
@pytest.mark.parametrize("value", BAD)
def test_the_bridge_over_the_as_filed_payload_refuses_a_bad_share_count(field, value):
    """The plane rebuilds the facts as filed by the observation from the raw
    payload. Failed on e03b470: that rebuild raised, was taken as "the
    mapper refuses", and the bridge read no filing at all."""
    ds, _ = build_dataset(_ko(), "KO")  # the report's own dataset is not under test
    plane = compute_plane(ds, LoadedObservation.of(_obs()), datetime.now(UTC).date(),
                          company_facts=_bridge_payload(field, value))
    bridge = plane.bridge
    assert plane.dataset is not None and bridge.fiscal_label == "FY2026Q1"
    assert bridge.shares.name == field and bridge.shares.value is None
    assert bridge.shares.note == INVALID_SHARE_COUNT
    assert bridge.market_cap.value is None and bridge.market_cap.note == INVALID_SHARE_COUNT
    assert bridge.ev is None and bridge.ev_reason == f"EV not asserted: {INVALID_SHARE_COUNT}"
    assert all(m.value is None for m in plane.multiples)
    (scenario,) = plane.expectations.scenarios
    assert scenario.value_per_share is None and scenario.upside is None
    assert scenario.reason == f"not computable: {INVALID_SHARE_COUNT}"


# --- the full report builds and publishes ----------------------------------------


@pytest.mark.parametrize("value", BAD)
def test_the_report_publishes_with_the_valuation_card_refusing_the_share_count(
    value, monkeypatch, tmp_path
):
    """The journal's real build, offline, with a price recorded. Failed on
    e03b470: the snapshot stage raised `UnmappablePayload` and nothing was
    published."""
    facts = _set_diluted(_bridge_payload("shares_diluted", value), MID, value)
    journal = tmp_path / "journal"
    write_observation(journal, _obs())
    monkeypatch.setattr(journal_reporting, "MARKET", journal / "market")
    monkeypatch.setattr(journal_reporting, "SecClient", lambda fresh=False: _Client(facts))
    monkeypatch.setattr(journal_reporting, "fetch_submissions_snapshot",
                        lambda ticker, client: _submissions())
    out, _ = journal_reporting.build_report(
        "KO", with_docs=False, out_dir=tmp_path / "reports", vintage=False,
    )
    report = out.read_text()
    assert "Decision Card — KO" in report
    assert f"EV not asserted: {INVALID_SHARE_COUNT}" in report
    assert "could not establish the report's quarters" not in report
    ledger = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    assert ledger.valuation is not None and ledger.valuation.state == "produced"


# --- the restatement scan and the vintage diff map it ----------------------------


@pytest.mark.parametrize("value", BAD)
def test_the_restatement_scan_rebuilds_the_report_quarters(value):
    """Failed on e03b470: the scan's rebuild raised and the derived check
    said it "could not establish the report's quarters". The report's
    selection is the unmodified payload's: one value moved, nothing else."""
    ds, diag = build_dataset(_ko(), "KO")
    scan = scan_restatements(_set_diluted(_ko(), MID, value),
                             selected_tags=diag.selected_series(),
                             scored_quarters=[p.period_end for p in ds.periods])
    assert scan.derived_gap is None


@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize("side", ["older", "newer"])
def test_the_vintage_diff_compares_a_snapshot_with_a_bad_share_count_as_scored(value, side):
    """Failed on e03b470: that snapshot "could not be mapped" and the diff
    fell back to raw rows."""
    good, bad = _ko(), _set_diluted(_ko(), MID, value)
    older, newer = (bad, good) if side == "older" else (good, bad)
    diff = diff_scored(older, newer)
    assert diff.canonical_unavailable is None
    assert all(c.scope != "raw" for c in diff.changes)


# --- the property tests draw a zero share count again ----------------------------


@pytest.mark.parametrize("field", ["shares_diluted", "shares_outstanding"])
def test_the_dataset_strategy_draws_a_zero_share_count(field):
    """#119 stopped `tests/strategies.py` drawing 0.0 for the share fields,
    which hid the schema's refusal from every property test."""
    find(period_datasets(), lambda ds: any(getattr(p, field) == 0.0 for p in ds.periods),
         settings=settings(max_examples=100, derandomize=True, database=None))
