"""A same-day rerun keeps the earlier report (Hermes audit round 7, finding 3:
rollback), and a replay is never "the latest report"."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.services.reporting.report_builder import ledger_path
from app.services.reporting.report_files import (
    GENERATION_LINE,
    STAGING_DIR,
    NotPublished,
    archive_existing,
    generation_of,
    is_live_report,
    read_live,
    replacing,
)

NOW = datetime(2026, 9, 26, 21, 5, 7, tzinfo=UTC)


def _run(dirpath, name="AAPL_2026-09-26.md", *, tag="first", audit=True):
    report = dirpath / name
    report.write_text(f"# {tag} report")
    ledger_path(report).write_text(f'{{"run": "{tag}"}}')
    if audit:
        report.with_name(f"{report.name.removesuffix('.md')}_audit.md").write_text(f"# {tag} audit")
    return report


class TestArchiveExisting:
    def test_nothing_there_moves_nothing_and_creates_no_archive(self, tmp_path):
        assert archive_existing(tmp_path / "AAPL_2026-09-26.md", now=NOW) == []
        assert not (tmp_path / "archive").exists()

    def test_the_whole_run_moves_together_under_one_stamp(self, tmp_path):
        report = _run(tmp_path)
        moved = archive_existing(report, now=NOW)
        arch = tmp_path / "archive"
        assert sorted(p.name for p in moved) == [
            "AAPL_2026-09-26.210507.ledger.json",
            "AAPL_2026-09-26.210507.md",
            "AAPL_2026-09-26.210507_audit.md",
        ]
        assert (arch / "AAPL_2026-09-26.210507.md").read_text() == "# first report"
        # The archived report still finds its own ledger and audit by name.
        assert ledger_path(arch / "AAPL_2026-09-26.210507.md").read_text() == '{"run": "first"}'
        # Nothing of the first run is left to sit beside the second.
        assert sorted(p.name for p in tmp_path.iterdir()) == [STAGING_DIR, "archive"]

    def test_a_second_archive_within_the_second_never_overwrites(self, tmp_path):
        first = _run(tmp_path, tag="first")
        archive_existing(first, now=NOW)
        second = _run(tmp_path, tag="second", audit=False)
        moved = archive_existing(second, now=NOW)
        arch = tmp_path / "archive"
        assert sorted(p.name for p in moved) == [
            "AAPL_2026-09-26.210507-1.ledger.json", "AAPL_2026-09-26.210507-1.md"]
        assert (arch / "AAPL_2026-09-26.210507.md").read_text() == "# first report"
        assert (arch / "AAPL_2026-09-26.210507-1.md").read_text() == "# second report"

    def test_when_every_stamp_is_taken_nothing_moves(self, tmp_path):
        arch = tmp_path / "archive"
        arch.mkdir()
        for n in range(100):
            tag = "210507" if n == 0 else f"210507-{n}"
            (arch / f"AAPL_2026-09-26.{tag}.md").write_text("")
        report = _run(tmp_path, audit=False)
        with pytest.raises(FileExistsError, match="100 runs"):
            archive_existing(report, now=NOW)
        assert report.read_text() == "# first report" and ledger_path(report).exists()

    def test_a_stamp_taken_by_any_companion_is_taken(self, tmp_path):
        """Only the earlier run's AUDIT holds the stamp: the next run must still
        not take it, or its audit would sit beside a report that is not its."""
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "AAPL_2026-09-26.210507_audit.md").write_text("# someone's audit")
        report = _run(tmp_path, audit=False)
        moved = archive_existing(report, now=NOW)
        assert {p.name for p in moved} == {
            "AAPL_2026-09-26.210507-1.md", "AAPL_2026-09-26.210507-1.ledger.json"}
        assert (arch / "AAPL_2026-09-26.210507_audit.md").read_text() == "# someone's audit"

    def test_a_ledger_alone_is_archived(self, tmp_path):
        """A run that died after writing its ledger but before its report must
        not leave that ledger to be read as the next report's."""
        report = tmp_path / "AAPL_2026-09-26.md"
        ledger_path(report).write_text("{}")
        moved = archive_existing(report, now=NOW)
        assert [p.name for p in moved] == ["AAPL_2026-09-26.210507.ledger.json"]
        assert not ledger_path(report).exists()

    def test_a_replay_keeps_its_replay_names(self, tmp_path):
        report = _run(tmp_path, name="AAPL_2025-06-30.replay.md")
        assert ledger_path(report).name == "AAPL_2025-06-30.replay.ledger.json"
        moved = archive_existing(report, now=NOW)
        assert sorted(p.name for p in moved) == [
            "AAPL_2025-06-30.replay.210507.ledger.json",
            "AAPL_2025-06-30.replay.210507.md",
            "AAPL_2025-06-30.replay.210507_audit.md",
        ]

    def test_another_ticker_or_day_is_untouched(self, tmp_path):
        other = _run(tmp_path, name="AAPL_2026-09-25.md")
        nvda = _run(tmp_path, name="NVDA_2026-09-26.md")
        archive_existing(tmp_path / "AAPL_2026-09-26.md", now=NOW)
        assert other.exists() and nvda.exists()
        assert not (tmp_path / "archive").exists()

    def test_the_stamp_is_utc_hms(self, tmp_path):
        report = _run(tmp_path, audit=False)
        moved = archive_existing(report, now=datetime(2026, 9, 26, 0, 0, 9, tzinfo=UTC))
        assert "AAPL_2026-09-26.000009.md" in {p.name for p in moved}


class TestIsLiveReport:
    def test_reports_are_live(self, tmp_path):
        assert is_live_report(tmp_path / "AAPL_2026-09-26.md")

    def test_audits_and_replays_are_not(self, tmp_path):
        assert not is_live_report(tmp_path / "AAPL_2026-09-26_audit.md")
        assert not is_live_report(tmp_path / "AAPL_2025-06-30.replay.md")
        assert not is_live_report(tmp_path / "AAPL_2025-06-30.replay_audit.md")


# --- replacing: build off to the side, publish only a finished rebuild ------------
# Hermes audit round 8, finding 2: archiving BEFORE the build left no live
# report or ledger behind a rebuild that failed.


def _stage(staged, tag, *, ledger=True, report=True):
    """What a rebuild writes: its report and its ledger."""
    if ledger:
        staged.ledger.write_text(json.dumps({"run": tag}))
    if report:
        staged.report.write_text(f"# {tag} report\n")


def _leftovers(dirpath):
    """Staged files left behind; the publish locks are meant to stay."""
    return [p for p in (dirpath / STAGING_DIR).glob("*") if p.suffix != ".lock"]


def _live(dirpath, name="AAPL_2026-09-26.md"):
    report = dirpath / name
    return {p.name: p.read_bytes() for p in (
        report, ledger_path(report),
        report.with_name(f"{name.removesuffix('.md')}_audit.md")) if p.exists()}


class TestReplacing:
    def test_a_failed_rebuild_leaves_the_live_run_exactly_as_it_was(self, tmp_path):
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(RuntimeError, match="build failed"):
            with replacing(report, now=NOW) as staged:
                staged.ledger.write_text('{"run": "half-built"}')
                raise RuntimeError("build failed")
        assert _live(tmp_path) == before and len(before) == 3
        assert not (tmp_path / "archive").exists()
        assert not _leftovers(tmp_path)

    def test_a_rebuild_that_writes_no_report_publishes_nothing(self, tmp_path):
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="wrote no report"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "orphan", report=False)
        assert _live(tmp_path) == before
        assert not (tmp_path / "archive").exists()
        assert not _leftovers(tmp_path)

    def test_a_finished_rebuild_archives_the_earlier_run_and_goes_live(self, tmp_path):
        report = _run(tmp_path)
        with replacing(report, now=NOW) as staged:
            assert STAGING_DIR in staged.report.parts and not staged.report.exists()
            assert report.exists()  # still live while the rebuild runs
            _stage(staged, "second")
        assert report.read_text().startswith("# second report")
        assert json.loads(ledger_path(report).read_text())["run"] == "second"
        assert generation_of(report) == generation_of(ledger_path(report)) == staged.generation_id
        # the earlier run's audit is not left beside the new report
        assert not report.with_name("AAPL_2026-09-26_audit.md").exists()
        arch = tmp_path / "archive"
        assert sorted(p.name for p in staged.archived) == [
            "AAPL_2026-09-26.210507.ledger.json",
            "AAPL_2026-09-26.210507.md",
            "AAPL_2026-09-26.210507_audit.md",
        ]
        assert (arch / "AAPL_2026-09-26.210507.md").read_text() == "# first report"
        assert (arch / "AAPL_2026-09-26.210507_audit.md").read_text() == "# first audit"
        assert not _leftovers(tmp_path)

    def test_a_first_run_archives_nothing(self, tmp_path):
        report = tmp_path / "AAPL_2026-09-26.md"
        with replacing(report, now=NOW) as staged:
            _stage(staged, "first")
        assert staged.archived == [] and not (tmp_path / "archive").exists()
        assert report.read_text().startswith("# first") and ledger_path(report).exists()

    def test_two_rebuilds_within_a_second_archive_under_two_stamps(self, tmp_path):
        report = _run(tmp_path, audit=False)
        for n in ("second", "third"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, n)
        arch = tmp_path / "archive"
        assert (arch / "AAPL_2026-09-26.210507.md").read_text() == "# first report"
        assert (arch / "AAPL_2026-09-26.210507-1.md").read_text().startswith("# second report")
        assert report.read_text().startswith("# third report")

    def test_staged_files_are_invisible_to_the_live_report_globs(self, tmp_path):
        report = _run(tmp_path, audit=False)
        with replacing(report, now=NOW) as staged:
            _stage(staged, "second")
            live = [p.name for p in tmp_path.glob("AAPL_*.md") if is_live_report(p)]
            assert live == ["AAPL_2026-09-26.md"]

    def test_a_rebuild_committing_leaves_another_rebuilds_staging_in_place(self, tmp_path):
        """Round-9 audit F1: rebuilds of different tickers share `.staging`. A
        rebuild that committed while another had not yet written anything
        removed the (empty) directory, and the other's report write failed."""
        first = _run(tmp_path, audit=False)
        other = tmp_path / "NVDA_2026-09-26.md"
        with replacing(other, now=NOW) as staged_other:  # mid-build, nothing written yet
            with replacing(first, now=NOW) as staged_first:
                _stage(staged_first, "second")
            _stage(staged_other, "NVDA")
        assert first.read_text().startswith("# second report")
        assert other.read_text().startswith("# NVDA report")

    def test_a_concurrent_rebuilds_staging_is_left_alone(self, tmp_path):
        report = _run(tmp_path, audit=False)
        other = tmp_path / STAGING_DIR / "someone-else.md"
        with replacing(report, now=NOW) as staged:
            other.write_text("# in progress")
            _stage(staged, "second")
        assert other.read_text() == "# in progress"


# --- Hermes deep audit, finding 2: a run without its ledger is not published -------


class TestAWholeGenerationOrNothing:
    def test_a_rebuild_without_a_ledger_publishes_nothing(self, tmp_path):
        """It used to publish the report alone, archive the complete earlier
        pair and delete the live ledger: a run with no evidence went live."""
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="no evidence ledger"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second", ledger=False)  # the ledger build failed
        assert _live(tmp_path) == before and len(before) == 3
        assert not (tmp_path / "archive").exists()
        assert not _leftovers(tmp_path)

    @pytest.mark.parametrize("stale", ["report", "ledger"])
    def test_a_file_naming_another_generation_publishes_nothing(self, tmp_path, stale):
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="names generation"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
                if stale == "report":
                    staged.report.write_text(f"# second\n{GENERATION_LINE}{'0' * 32}\n")
                else:
                    staged.ledger.write_text(json.dumps({"generation_id": "0" * 32}))
        assert _live(tmp_path) == before
        assert not (tmp_path / "archive").exists()

    def test_the_publish_stamps_the_report_and_ledger_with_one_generation(self, tmp_path):
        report = tmp_path / "AAPL_2026-09-26.md"
        with replacing(report, now=NOW) as staged:
            _stage(staged, "first")
        live = read_live(report)
        assert live is not None and live.generation_id == staged.generation_id
        assert live.ledger == ledger_path(report) and live.audit is None and not live.stale
        assert live.text.endswith(
            f"{GENERATION_LINE}{staged.generation_id} (this report, its evidence ledger "
            "and its audit carry the same id)\n")
        assert json.loads(ledger_path(report).read_text()) == {
            "run": "first", "generation_id": staged.generation_id}

    @pytest.mark.parametrize("bad", ["not json", "[1, 2]"])
    def test_a_ledger_that_is_not_a_json_object_publishes_nothing(self, tmp_path, bad):
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="evidence ledger is not"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
                staged.ledger.write_text(bad)
        assert _live(tmp_path) == before


# --- Hermes deep audit, finding 1: two publishers of one report ----------------------


def _publisher(args):
    """One rebuild in its own process: stage, wait for the rival, publish.
    `os.replace` is slowed so an unlocked publish would interleave."""
    import os
    import time

    import app.services.reporting.report_files as rf

    report, tag, barrier = args
    real = os.replace

    def slow(src, dst):
        real(src, dst)
        time.sleep(0.2)

    rf.os.replace = slow  # this process only
    with replacing(report) as staged:
        _stage(staged, tag)
        barrier.wait()
    return staged.generation_id


def test_two_processes_publishing_one_report_leave_one_whole_generation(tmp_path):
    """Two rebuilds of one report publishing at once (a manual rerun and the
    watcher on a filing night) interleaved their `os.replace` calls and left
    report A beside ledger B: each file valid, the pair from two runs."""
    import multiprocessing as mp

    report = _run(tmp_path, audit=False)
    ctx = mp.get_context("fork")
    with ctx.Manager() as manager:
        barrier = manager.Barrier(2)
        with ctx.Pool(2) as pool:
            ids = pool.map(_publisher, [(report, "A", barrier), (report, "B", barrier)])
    live = read_live(report)
    assert live is not None and live.generation_id in ids
    assert generation_of(ledger_path(report)) == live.generation_id
    # The earlier generation went to the archive whole, once per publish.
    archived = sorted(p.name for p in (tmp_path / "archive").iterdir())
    assert len([n for n in archived if n.endswith(".ledger.json")]) == 2
    assert not _leftovers(tmp_path)


class TestReadLive:
    def test_an_audit_of_another_generation_is_stale_not_the_reports(self, tmp_path):
        report = tmp_path / "AAPL_2026-09-26.md"
        with replacing(report, now=NOW) as staged:
            _stage(staged, "first")
        audit = report.with_name("AAPL_2026-09-26_audit.md")
        audit.write_text(f"<!-- generation: {'f' * 32} -->\n\n# an earlier run's audit")
        live = read_live(report)
        assert live is not None and live.audit is None and live.stale == (audit,)
        audit.write_text(f"<!-- generation: {staged.generation_id} -->\n\n# its audit")
        assert read_live(report).audit == audit

    def test_files_from_before_generations_still_pair(self, tmp_path):
        report = _run(tmp_path, audit=False)
        live = read_live(report)
        assert live is not None and live.generation_id is None
        assert live.ledger == ledger_path(report) and not live.stale

    def test_no_report_reads_as_none(self, tmp_path):
        assert read_live(tmp_path / "AAPL_2026-09-26.md") is None


# --- round-9 independent review: the commit phase itself fails --------------------


class TestReplacingCommitFailures:
    def _fail_on(self, monkeypatch, fn_name, match, exc):
        import app.services.reporting.report_files as rf

        real = getattr(rf.os if fn_name == "replace" else rf.shutil, fn_name)

        def flaky(src, dst, *a, **k):
            if str(dst).endswith(match):
                raise exc
            return real(src, dst, *a, **k)

        monkeypatch.setattr(rf.os if fn_name == "replace" else rf.shutil, fn_name, flaky)

    def test_an_interrupt_between_ledger_and_report_rolls_back(self, tmp_path, monkeypatch):
        """R1: the new ledger was published and the report was not; the pair
        stayed mismatched and the next rebuild archived it as one run."""
        report = _run(tmp_path)
        before = _live(tmp_path)
        self._fail_on(monkeypatch, "replace", "AAPL_2026-09-26.md", KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
        assert _live(tmp_path) == before  # report, ledger AND audit as they were
        assert not list((tmp_path / "archive").glob("*"))  # the live run is not also archived
        assert not _leftovers(tmp_path)

    def test_a_first_run_that_fails_to_publish_leaves_no_orphan_ledger(self, tmp_path, monkeypatch):
        report = tmp_path / "AAPL_2026-09-26.md"
        self._fail_on(monkeypatch, "replace", "AAPL_2026-09-26.md", OSError("disk"))
        with pytest.raises(OSError, match="disk"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "first")
        assert not report.exists() and not ledger_path(report).exists()

    def test_an_audit_that_cannot_be_removed_publishes_nothing(self, tmp_path, monkeypatch):
        """R2: the earlier audit was removed AFTER the new report went live,
        so a failure there left it beside the new report (the round-7 pairing
        defect) and the caller saw an error for a published rebuild."""
        report = _run(tmp_path)
        before = _live(tmp_path)
        audit = tmp_path / "AAPL_2026-09-26_audit.md"
        real_unlink = type(audit).unlink

        def stuck(self, *a, **k):
            if self == audit:
                raise PermissionError("audit locked")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(type(audit), "unlink", stuck)
        with pytest.raises(PermissionError):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
        monkeypatch.undo()
        assert _live(tmp_path) == before
        assert not list((tmp_path / "archive").glob("*"))

    def test_a_failed_archive_copy_leaves_no_partial_archived_run(self, tmp_path, monkeypatch):
        """R3: a copy that failed part-way left an archived report with no
        ledger or audit, which then looked like a whole archived run."""
        report = _run(tmp_path)
        before = _live(tmp_path)
        self._fail_on(monkeypatch, "copy2", ".ledger.json", OSError("ENOSPC"))
        with pytest.raises(OSError, match="ENOSPC"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
        assert _live(tmp_path) == before
        assert not list((tmp_path / "archive").glob("*"))


def test_a_ledger_failure_is_logged_under_its_report_and_raised(tmp_path, monkeypatch, caplog):
    """R4: built into staging, a failed ledger logged only
    `<token>.ledger.json`, and the operator could not tell which report. It
    is now also raised: the run is not published without it."""
    from datetime import date

    from app.services.reporting import report_builder

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", False)
    with caplog.at_level("ERROR"), pytest.raises(NotPublished, match="AAPL 2026-09-26"):
        report_builder.write_ledger(
            tmp_path / ".staging" / "0123456789ab.ledger.json",
            ticker="AAPL", report_date=date(2026, 9, 26), result=None)
    assert "AAPL 2026-09-26" in caplog.text
