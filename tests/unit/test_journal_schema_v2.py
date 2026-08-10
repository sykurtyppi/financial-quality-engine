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

    def test_whitespace_only_thesis_rejected(self):
        # Round-10 finding 6: min_length=1 does not catch "   ". Also
        # `\t\n` etc. — none of these are a preregistered commitment.
        for bad in ("   ", "\t\t", "\n", " \t\n "):
            with pytest.raises(ValidationError, match="non-whitespace"):
                BeforeBlock(thesis=bad, conviction=3, intended_action="hold",
                            assumptions=[_min_assumption()])

    def test_thesis_stripped_on_construction(self):
        # Leading/trailing whitespace normalized; hash still deterministic.
        b1 = BeforeBlock(thesis="  hello world  ", conviction=3,
                         intended_action="hold", assumptions=[_min_assumption()])
        b2 = BeforeBlock(thesis="hello world", conviction=3,
                         intended_action="hold", assumptions=[_min_assumption()])
        assert b1.thesis == "hello world"
        assert hash_before(b1) == hash_before(b2)

    def test_within_comparator_refused_at_lock(self):
        # Round-10 finding 6: the resolver has no way to evaluate `within`, so
        # locking a claim with that comparator commits the user to something
        # that can never resolve. can_lock refuses.
        before = BeforeBlock(
            thesis="X", conviction=3, intended_action="hold",
            assumptions=[_min_assumption(comparator="within")],
        )
        ok, reason = can_lock(before)
        assert ok is False and "within" in reason and "resolvable" in reason

    def test_p_outcome_requires_outcome_definition_at_model_level(self):
        # Round-11 finding 1: BEFORE is hash-locked at openv2 time, so users
        # cannot add outcome_definition later. The pairing is enforced by the
        # model itself — no p_outcome without a preregistered event.
        with pytest.raises(ValidationError, match="outcome_definition"):
            BeforeBlock(
                thesis="x", conviction=3, intended_action="hold",
                assumptions=[_min_assumption()],
                p_outcome=0.7,  # no outcome_definition -> ValidationError
            )

    def test_p_outcome_with_outcome_definition_ok(self):
        # And the same-shape entry with the pair is fine.
        b = BeforeBlock(
            thesis="x", conviction=3, intended_action="hold",
            assumptions=[_min_assumption()],
            outcome_definition="Q2 revenue > 165M",
            p_outcome=0.7,
        )
        assert b.p_outcome == 0.7 and b.outcome_definition == "Q2 revenue > 165M"

    def test_whitespace_outcome_definition_treated_as_none(self):
        # Round-11 finding 1: "   " is not a definition. A blank/whitespace
        # outcome_definition normalizes to None; paired with p_outcome, that
        # trips the pairing validator (which is the whole point).
        with pytest.raises(ValidationError, match="outcome_definition"):
            BeforeBlock(
                thesis="x", conviction=3, intended_action="hold",
                assumptions=[_min_assumption()],
                outcome_definition="   ",
                p_outcome=0.7,
            )

    def test_symbolic_threshold_blank_rejected_at_field(self):
        # Round-11 finding 3: an empty symbolic threshold used to pass through.
        with pytest.raises(ValidationError, match="non-blank"):
            _min_assumption(threshold="")
        with pytest.raises(ValidationError, match="non-blank"):
            _min_assumption(threshold="   ")

    def test_symbolic_threshold_unknown_keyword_refused_at_lock(self):
        # Round-11 finding 3: unrecognized keywords are refused at lock — the
        # resolver has no defined semantics for `strong`.
        before = BeforeBlock(
            thesis="x", conviction=3, intended_action="hold",
            assumptions=[_min_assumption(metric="cfo", threshold="strong")],
        )
        ok, reason = can_lock(before)
        assert ok is False and "strong" in reason and "unrecognized" in reason

    def test_symbolic_threshold_mismatched_comparator_refused(self):
        # Round-11 finding 3 core defect: `cfo < positive` is a contradiction
        # (positive means > 0). Old code let it lock AND resolved `met` when
        # CFO was positive. can_lock now rejects the pair itself.
        before = BeforeBlock(
            thesis="x", conviction=3, intended_action="hold",
            assumptions=[_min_assumption(metric="cfo", comparator="<", threshold="positive")],
        )
        ok, reason = can_lock(before)
        assert ok is False and "positive" in reason and ">" in reason

    def test_symbolic_threshold_canonical_pair_ok(self):
        # Only the canonical pair passes: `cfo > positive`.
        before = BeforeBlock(
            thesis="x", conviction=3, intended_action="hold",
            assumptions=[_min_assumption(metric="cfo", comparator=">", threshold="positive")],
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
        # Round-10 finding 1: lock does NOT stamp `reported`. That belongs to
        # the report step (`cmd_report`), which happens after the engine runs.
        assert locked.reported is None
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
            outcome_definition="MXL Q2 revenue > 165M by 2026-08-15",
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


class TestStorageRoundtrip:
    def test_save_load_v2_and_is_v2(self, tmp_path, monkeypatch):
        from app.services.journal import store

        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        locked = lock_entry(_min_entry(catalyst="Q2 print"))
        path = store.save_v2(locked)
        assert path.exists()
        assert store.is_v2(path) is True
        back = store.load_v2(path)
        assert back.model_dump() == locked.model_dump()
        assert verify_lock(back) is True

    def test_save_v2_refuses_to_overwrite_v1(self, tmp_path, monkeypatch):
        from app.services.journal import store

        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        v1_path = tmp_path / "MXL_2026-07-27.md"
        v1_path.write_text("# MXL — 2026-07-27\nopened: ...\nthesis: v1 text\n")
        assert store.is_v2(v1_path) is False
        with pytest.raises(FileExistsError, match="v1 entry"):
            store.save_v2(lock_entry(_min_entry()), v1_path)


class TestResolutionHelpers:
    def test_open_indices_are_the_unresolved_ones(self):
        from app.services.journal.schema_v2 import Resolution, open_assumption_indices

        entry = lock_entry(_min_entry())
        entry = entry.model_copy(update={
            "before": entry.before.model_copy(update={
                "assumptions": [_min_assumption(), _min_assumption(metric="cfo"), _min_assumption(metric="ni")],
            })
        })
        assert open_assumption_indices(entry) == [0, 1, 2]
        with_one = entry.model_copy(update={
            "resolutions": [Resolution(assumption_index=1, state="met")],
        })
        assert open_assumption_indices(with_one) == [0, 2]

    def test_pending_resolution_does_not_close_the_queue(self):
        # Round-11 finding 4: even if a pending row is somehow persisted, it
        # must NOT close the queue — pending is retryable, not terminal.
        from app.services.journal.schema_v2 import Resolution, open_assumption_indices

        entry = lock_entry(_min_entry())
        # Persist a pending row for assumption 0 (a path the CLI avoids, but
        # any programmatic caller could take).
        with_pending = entry.model_copy(update={
            "resolutions": [Resolution(assumption_index=0, state="pending",
                                       note="filing not yet arrived")],
        })
        # 0 is still open — the auditor's reproduction fails now (was: []).
        assert open_assumption_indices(with_pending) == [0]

    def test_terminal_resolution_supersedes_earlier_pending(self):
        # A pending row followed by a met row for the same index resolves the
        # assumption terminally. add_resolution replaces same-index rows.
        from app.services.journal.schema_v2 import Resolution, add_resolution, open_assumption_indices

        entry = lock_entry(_min_entry())
        entry = add_resolution(entry, Resolution(assumption_index=0, state="pending"))
        assert open_assumption_indices(entry) == [0]
        entry = add_resolution(entry, Resolution(assumption_index=0, state="met", observed=170.0))
        assert open_assumption_indices(entry) == []

    def test_add_resolution_is_idempotent(self):
        from app.services.journal.schema_v2 import Resolution, add_resolution

        entry = lock_entry(_min_entry())
        entry = add_resolution(entry, Resolution(assumption_index=0, state="met", observed=170.0))
        assert len(entry.resolutions) == 1 and entry.resolutions[0].observed == 170.0
        # A second call for the same index REPLACES, not appends.
        entry = add_resolution(entry, Resolution(assumption_index=0, state="violated", observed=140.0))
        assert len(entry.resolutions) == 1 and entry.resolutions[0].state == "violated"

    def test_add_resolution_preserves_lock(self):
        from app.services.journal.schema_v2 import Resolution, add_resolution

        entry = lock_entry(_min_entry())
        assert verify_lock(entry) is True
        after = add_resolution(entry, Resolution(assumption_index=0, state="met"))
        assert verify_lock(after) is True  # BEFORE untouched


class TestV2Tally:
    def _seed(self, tmp_path, monkeypatch):
        from app.services.journal import store

        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        return store

    def test_empty_tally(self, tmp_path, monkeypatch):
        store = self._seed(tmp_path, monkeypatch)
        t = store.v2_tally()
        assert t["total"] == 0 and t["locked"] == 0 and t["lock_broken"] == 0
        assert t["open_assumptions"] == 0 and t["brier"] is None

    def test_v1_files_ignored_by_v2_tally(self, tmp_path, monkeypatch):
        store = self._seed(tmp_path, monkeypatch)
        (tmp_path / "OLD_2025-01-01.md").write_text("# OLD\nthesis: x\n")
        assert store.v2_tally()["total"] == 0

    def test_locked_and_resolutions_counted(self, tmp_path, monkeypatch):
        from app.services.journal.schema_v2 import Resolution, add_resolution

        store = self._seed(tmp_path, monkeypatch)
        entry = lock_entry(_min_entry())
        entry = add_resolution(entry, Resolution(assumption_index=0, state="met", observed=170.0))
        store.save_v2(entry)
        t = store.v2_tally()
        assert t["total"] == 1 and t["locked"] == 1 and t["lock_broken"] == 0
        assert t["resolutions"]["met"] == 1
        assert t["open_assumptions"] == 0

    def test_overdue_open_assumption_surfaced(self, tmp_path, monkeypatch):
        store = self._seed(tmp_path, monkeypatch)
        # resolve_by is in the past AND unresolved -> shows as overdue.
        entry = lock_entry(_min_entry())
        # override resolve_by via a fresh assumption
        past = _min_before(assumptions=[_min_assumption(resolve_by=date(2020, 1, 1))])
        entry = entry.model_copy(update={"before": past, "before_sha256": None, "locked_at": None})
        entry = lock_entry(entry)
        store.save_v2(entry)
        t = store.v2_tally(today_iso="2026-08-10")
        assert t["overdue_assumptions"] and t["overdue_assumptions"][0][2] == "revenue"

    def test_brier_deferred_below_min_n(self, tmp_path, monkeypatch):
        # Round-10 finding 3: Brier requires an explicit outcome_definition +
        # observed y (not verdict). A single valid pair is still below the
        # Murphy floor -> brier None, but the raw pair count is reported.
        store = self._seed(tmp_path, monkeypatch)
        entry = lock_entry(_min_entry(
            outcome_definition="Q2 revenue > 165M",
            p_outcome=0.7,
        ))
        entry = entry.model_copy(update={
            "outcome": entry.outcome.model_copy(update={"y": True}),
        })
        store.save_v2(entry)
        t = store.v2_tally()
        assert t["brier"] is None
        assert t["resolved_with_p_outcome"] == 1

    def test_brier_ignores_verdict_when_y_absent(self, tmp_path, monkeypatch):
        # Round-10 finding 3 + round-11 finding 1: an entry with a valid
        # (outcome_definition, p_outcome) pair but no observed y is not
        # eligible for Brier — verdict=helped/hurt does not stand in for y.
        store = self._seed(tmp_path, monkeypatch)
        entry = lock_entry(_min_entry(
            outcome_definition="Q2 revenue > 165M",
            p_outcome=0.7,
        ))
        entry = entry.model_copy(update={
            "outcome": entry.outcome.model_copy(update={"verdict": "helped"}),
        })
        store.save_v2(entry)
        t = store.v2_tally()
        assert t["resolved_with_p_outcome"] == 0

    def test_broken_lock_excluded_from_resolutions_and_brier(self, tmp_path, monkeypatch):
        # Round-10 finding 2: tampered entries are audited but excluded from
        # every inferential metric.
        store = self._seed(tmp_path, monkeypatch)
        entry = lock_entry(_min_entry(
            outcome_definition="Q2 revenue > 165M",
            p_outcome=0.7,
        ))
        path = store.save_v2(entry)

        # Add a resolution + outcome.y, then tamper the thesis under the old hash.
        from app.services.journal.schema_v2 import Resolution, add_resolution, render_entry
        with_res = add_resolution(entry, Resolution(assumption_index=0, state="met", observed=170.0))
        with_res = with_res.model_copy(update={
            "outcome": with_res.outcome.model_copy(update={"y": True}),
        })
        tampered = with_res.model_copy(update={
            "before": with_res.before.model_copy(update={"thesis": "hindsight rewrite"}),
        })
        tampered = tampered.model_copy(update={"before_sha256": entry.before_sha256})
        path.write_text(render_entry(tampered))

        t = store.v2_tally()
        assert t["lock_broken"] == 1
        # The tampered entry HAS a resolution and a (p_outcome, y) pair, but
        # NONE of that should show up in the inferential metrics.
        assert t["resolutions"]["met"] == 0
        assert t["resolved_with_p_outcome"] == 0

    def test_lock_broken_flagged(self, tmp_path, monkeypatch):
        # Tamper an on-disk entry and confirm v2_tally counts it as lock_broken.
        store = self._seed(tmp_path, monkeypatch)
        entry = lock_entry(_min_entry())
        path = store.save_v2(entry)
        # Rewrite the file with an edited thesis but the OLD hash.
        tampered = entry.model_copy(update={
            "before": entry.before.model_copy(update={"thesis": "hindsight edit"}),
        })
        # Preserve the original hash (simulate a hand-edit).
        tampered = tampered.model_copy(update={"before_sha256": entry.before_sha256})
        from app.services.journal.schema_v2 import render_entry
        path.write_text(render_entry(tampered))
        t = store.v2_tally()
        assert t["lock_broken"] == 1


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
