"""Decision-journal schema v2 tests (P1-E). Verifies the anti-annoyance lock
rule, tamper detection via BEFORE-block hash, roundtrip serialization, and that
v1 entries are cleanly rejected (parsed by a separate path)."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.services.journal.schema_v2 import (
    SCHEMA_VERSION,
    Assumption,
    BeforeBlock,
    EntryV2,
    can_lock,
    hash_before,
    is_locked,
    lock_entry,
    parse_entry,
    render_entry,
    verify_lock,
)


def _min_assumption(**overrides) -> Assumption:
    base = dict(
        metric="revenue",
        comparator=">",
        threshold=1_000_000_000.0,
        window="FY2026Q2",
        source="10-Q",
        resolve_by=date(2026, 8, 15),
    )
    base.update(overrides)
    return Assumption(**base)


def _min_before(**overrides) -> BeforeBlock:
    base = dict(
        thesis="AI-optical DSP thesis: MXL is one of three suppliers.",
        conviction=4,
        intended_action="hold",
        assumptions=[_min_assumption()],
    )
    base.update(overrides)
    return BeforeBlock(**base)


def _min_entry(**before_overrides) -> EntryV2:
    return EntryV2(
        ticker="MXL",
        day=date(2026, 7, 27),
        opened=datetime(2026, 7, 27, 9, 41, 11, tzinfo=timezone.utc),
        before=_min_before(**before_overrides),
    )


class TestAntiAnnoyanceLockRule:
    def test_thesis_required_by_pydantic(self):
        # thesis pydantic-required (min_length=1); a blank thesis can't construct.
        with pytest.raises(ValidationError):
            BeforeBlock(thesis="", conviction=3, intended_action="hold",
                        assumptions=[_min_assumption()])

    def test_conviction_range_enforced(self):
        with pytest.raises(ValidationError):
            BeforeBlock(thesis="x", conviction=6, intended_action="hold",
                        assumptions=[_min_assumption()])
        with pytest.raises(ValidationError):
            BeforeBlock(thesis="x", conviction=0, intended_action="hold",
                        assumptions=[_min_assumption()])

    def test_at_least_one_assumption_required_to_lock(self):
        # A well-formed BEFORE without any assumption row can EXIST (entry can be
        # drafted) but cannot LOCK — the specificity floor.
        before = BeforeBlock(thesis="x", conviction=3, intended_action="hold", assumptions=[])
        ok, reason = can_lock(before)
        assert ok is False and "assumption" in reason

    def test_minimum_lock_only_needs_three_fields(self):
        # The anti-annoyance rule in action: thesis + conviction + one assumption.
        before = BeforeBlock(
            thesis="I think X",
            conviction=3,
            intended_action="hold",
            assumptions=[_min_assumption()],
        )
        ok, reason = can_lock(before)
        assert ok is True and reason is None


class TestLockAndTamperDetection:
    def test_lock_stamps_hash_and_timestamps(self):
        e = _min_entry()
        assert not is_locked(e)
        locked = lock_entry(e, now=datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc))
        assert is_locked(locked)
        assert locked.before_sha256 is not None and len(locked.before_sha256) == 64
        assert locked.locked_at == datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)
        assert locked.reported is not None  # stamped on lock if unset
        assert verify_lock(locked) is True

    def test_verify_fails_after_before_edit(self):
        locked = lock_entry(_min_entry())
        # Tamper: edit the thesis after locking.
        tampered = locked.model_copy(update={
            "before": locked.before.model_copy(update={"thesis": "different thesis"})
        })
        assert verify_lock(tampered) is False

    def test_verify_fails_on_assumption_edit(self):
        locked = lock_entry(_min_entry())
        tampered = locked.model_copy(update={
            "before": locked.before.model_copy(update={
                "assumptions": [_min_assumption(threshold=999_999_999.0)],
            })
        })
        assert verify_lock(tampered) is False

    def test_after_and_outcome_edits_do_not_invalidate_lock(self):
        # Only the BEFORE block is hashed — filling AFTER/OUTCOME must not break
        # the lock (that would defeat the point of the AFTER block).
        locked = lock_entry(_min_entry())
        updated = locked.model_copy(update={
            "after": locked.after.model_copy(update={
                "impact": "changed_confidence", "conviction_after": 4,
            }),
        })
        assert verify_lock(updated) is True

    def test_cannot_lock_without_assumption(self):
        e = EntryV2(
            ticker="X", day=date(2026, 1, 1),
            opened=datetime(2026, 1, 1, tzinfo=timezone.utc),
            before=BeforeBlock(thesis="x", conviction=3, intended_action="hold", assumptions=[]),
        )
        with pytest.raises(ValueError, match="assumption"):
            lock_entry(e)

    def test_hash_is_deterministic_across_reconstruction(self):
        # Same BEFORE data -> same hash regardless of key insertion order.
        a = _min_before()
        b = BeforeBlock(**a.model_dump())
        assert hash_before(a) == hash_before(b)


class TestRoundtrip:
    def test_render_parse_roundtrip_preserves_all_fields(self):
        locked = lock_entry(_min_entry(
            catalyst="Q2 print 2026-07-28",
            contamination="discussed fundamentals with Claude before lock",
            p_outcome=0.65,
            reference_class="small-cap semis with hyperscaler exposure",
            falsifiers=["Q2 revenue < 155M", "gross margin < 55%"],
        ))
        text = render_entry(locked)
        assert "---json" in text  # front-matter fence
        assert "# MXL — 2026-07-27" in text  # human header
        back = parse_entry(text)
        assert back.model_dump() == locked.model_dump()
        assert verify_lock(back) is True

    def test_body_contains_thesis_prose_for_humans(self):
        e = lock_entry(_min_entry())
        text = render_entry(e)
        assert "## Thesis (BEFORE)" in text
        assert e.before.thesis in text

    def test_v1_style_markdown_rejected_with_clear_error(self):
        v1_text = (
            "# MXL — 2026-07-27\nopened: 2026-07-27T09:41:11Z\n"
            "## BEFORE\nthesis: x\nconviction: 4\n"
        )
        with pytest.raises(ValueError, match="v1"):
            parse_entry(v1_text)

    def test_missing_front_matter_rejected(self):
        with pytest.raises(ValueError, match="front-matter"):
            parse_entry("no fences here")

    def test_malformed_json_front_matter_rejected(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_entry("---json\n{not json\n---\n\n# X\n")


class TestSchemaSurface:
    def test_schema_version_is_two(self):
        assert SCHEMA_VERSION == 2
        assert _min_entry().schema_version == 2

    def test_ticker_uppercased(self):
        e = EntryV2(ticker="mxl", day=date(2026, 1, 1),
                    opened=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    before=_min_before())
        assert e.ticker == "MXL"

    def test_falsifiers_dropped_when_blank(self):
        before = _min_before(falsifiers=["  ", "real one", ""])
        assert before.falsifiers == ["real one"]
