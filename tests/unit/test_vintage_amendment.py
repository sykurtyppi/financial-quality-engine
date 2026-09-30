"""An amendment landing between two snapshots is not a silent revision.

Found by the earnings-night drill (scripts/drill.py, step 3): a 10-Q/A filed
after the first snapshot moved the scored revenue by 10%, and the report put
it on the card twice, once as `Restatement (10-Q/A) …` (correct) and once as
a Tier-1 `Silent revision: …` line, under an appendix that said nothing there
had an amended filing behind it.

A move is explained by a filing when the newer snapshot still carries the
fact the older value was read from (same accession, period and value) beside
the later filing the value now comes from: the within-snapshot restatement
scan reads that pair from filing history. A value changed in place (same
accession), or a new filing whose original is gone from the newer snapshot,
stays silent, and is promoted as before.
"""

from __future__ import annotations

import copy
from datetime import date

import pytest

from app.services.ingestion.vintages import (
    diff_scored,
    diff_vintages,
    render_changes,
    silent_revision_tier1_lines,
)
from tests.fixtures.selection_cases import QUARTER_ENDS, quarter
from tests.unit.test_vintage_composed import _every_field

Q = QUARTER_ENDS[-3]  # 2024-06-30, a discrete 10-Q quarter inside the window
FLOOR = date(2022, 1, 1)
AMENDMENT = "0000000001-25-000999"


def _revenue_rows(facts: dict) -> list[dict]:
    return facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
        "units"]["USD"]


def _original(facts: dict) -> dict:
    three_months = quarter(Q, 0.0)["start"]
    (row,) = (r for r in _revenue_rows(facts) if r["end"] == Q.isoformat()
              and r.get("start") == three_months and r["form"] == "10-Q")
    return row


@pytest.fixture
def older() -> dict:
    return _every_field(composites=False)


def _amended(older: dict, *, keep_original: bool = True, factor: float = 1.10,
             accession: str = AMENDMENT, form: str = "10-Q/A") -> dict:
    newer = copy.deepcopy(older)
    orig = _original(newer)
    rows = _revenue_rows(newer)
    new = {**quarter(Q, round(orig["val"] * factor, 2)), "form": form, "accn": accession,
           "filed": "2025-03-01"}
    if not keep_original:
        rows.remove(orig)
    rows.append(new)
    return newer


def _revenue_change(older: dict, newer: dict):
    (c,) = (c for c in diff_scored(older, newer).changes
            if c.field_name == "revenue" and c.key.end == Q)
    return c


def test_an_amendment_beside_its_original_is_explained_not_silent(older):
    c = _revenue_change(older, _amended(older))
    assert c.kind == "revised" and c.pct_change == pytest.approx(0.10)
    assert (c.old_accession, c.new_accession, c.new_form) == (
        _original(older)["accn"], AMENDMENT, "10-Q/A")
    assert c.original_retained and c.explained_by_filing
    assert silent_revision_tier1_lines([c], "a", "b", period_since=FLOOR) == []


def test_a_later_ordinary_filing_beside_the_original_is_explained_too(older):
    """A comparative re-presented in a later 10-Q is in filing history as
    well (the restatement scan's non-amendment revisions)."""
    c = _revenue_change(older, _amended(older, form="10-Q"))
    assert c.explained_by_filing


def test_a_new_filing_whose_original_is_gone_stays_silent(older):
    """The original dropped from the newer snapshot: nothing within it shows
    the move, which is exactly what the vintage store exists to catch."""
    c = _revenue_change(older, _amended(older, keep_original=False))
    assert not c.original_retained and not c.explained_by_filing
    assert silent_revision_tier1_lines([c], "a", "b", period_since=FLOOR)


def test_a_value_changed_under_the_same_accession_stays_silent(older):
    newer = copy.deepcopy(older)
    _original(newer)["val"] *= 1.10
    c = _revenue_change(older, newer)
    assert c.old_accession == c.new_accession
    assert not c.explained_by_filing
    assert silent_revision_tier1_lines([c], "a", "b", period_since=FLOOR)


def test_an_original_carried_at_another_value_is_not_the_original(older):
    """Same accession kept, but its value rewritten, beside a new filing:
    the pair in filing history is not the pair the older snapshot saw."""
    newer = _amended(older)
    _original(newer)["val"] += 1
    c = _revenue_change(older, newer)
    assert not c.original_retained and not c.explained_by_filing


def test_the_raw_diff_applies_the_same_rule(older):
    explained = {c.field_name: c for c in diff_vintages(older, _amended(older))}
    silent = {c.field_name: c for c in diff_vintages(older, _amended(older, keep_original=False))}
    assert explained["revenue"].explained_by_filing
    assert not silent["revenue"].explained_by_filing


def test_a_withdrawal_is_never_explained(older):
    newer = copy.deepcopy(older)
    _revenue_rows(newer).remove(_original(newer))
    for c in diff_vintages(older, newer):
        assert not c.explained_by_filing


def test_the_appendix_lists_an_explained_move_apart_with_its_filing(older):
    c = _revenue_change(older, _amended(older))
    text = render_changes([c], "2025-02-01", "2025-03-02")
    assert "Nothing found here has an amended filing" not in text
    assert "No prior-period figure changed silently between these snapshots." in text
    assert "**Moved with a later filing (not silent).**" in text
    assert f"| 2025-03-01 10-Q/A {AMENDMENT} |" in text


def test_silent_and_explained_moves_each_keep_their_own_table(older):
    explained = _revenue_change(older, _amended(older))
    silent = _revenue_change(older, _amended(older, keep_original=False))
    text = render_changes([silent, explained], "2025-02-01", "2025-03-02")
    silent_part, filed_part = text.split("**Moved with a later filing (not silent).**")
    assert "Nothing found here has an amended filing" in silent_part
    assert silent_part.count("| revenue |") == 1 and filed_part.count("| revenue |") == 1
    assert AMENDMENT in filed_part and "Revised by" in filed_part


def test_another_filing_carrying_the_old_value_is_not_the_original(older):
    """Retention is of the FACT the older value was read from, by accession:
    a later comparative repeating the old number does not stand in for it."""
    newer = _amended(older, keep_original=False)
    orig = _original(older)
    _revenue_rows(newer).append({**orig, "accn": "0000000001-25-000500", "filed": "2025-02-01"})
    c = _revenue_change(older, newer)
    assert c.old_accession == orig["accn"]
    assert not c.original_retained and not c.explained_by_filing


def test_the_same_filings_fact_for_another_period_is_not_the_original(older):
    """The original's filing also carries a year-to-date fact ending on the
    same day. It survives in the newer snapshot while the quarter's own fact
    is gone: the quarter's original is not retained."""
    orig = _original(older)
    six_months = {**orig, "start": date(Q.year, 1, 1).isoformat(), "val": 7_777.0}
    _revenue_rows(older).append(six_months)
    newer = _amended(older, keep_original=False)
    c = _revenue_change(older, newer)
    assert c.old_accession == orig["accn"] and c.key.start is not None
    assert not c.original_retained and not c.explained_by_filing
