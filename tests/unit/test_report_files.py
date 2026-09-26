"""A same-day rerun keeps the earlier report (Hermes audit round 7, finding 3:
rollback), and a replay is never "the latest report"."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.reporting.report_builder import ledger_path
from app.services.reporting.report_files import (
    STAGING_DIR,
    archive_existing,
    is_live_report,
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
        assert sorted(p.name for p in tmp_path.iterdir()) == ["archive"]

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
        assert not (tmp_path / STAGING_DIR).exists()

    def test_a_rebuild_that_writes_no_report_publishes_nothing(self, tmp_path):
        report = _run(tmp_path)
        before = _live(tmp_path)
        with pytest.raises(RuntimeError, match="wrote no report"):
            with replacing(report, now=NOW) as staged:
                staged.ledger.write_text('{"run": "orphan"}')
        assert _live(tmp_path) == before
        assert not (tmp_path / "archive").exists()
        assert not (tmp_path / STAGING_DIR).exists()

    def test_a_finished_rebuild_archives_the_earlier_run_and_goes_live(self, tmp_path):
        report = _run(tmp_path)
        with replacing(report, now=NOW) as staged:
            assert STAGING_DIR in staged.report.parts and not staged.report.exists()
            assert report.exists()  # still live while the rebuild runs
            staged.ledger.write_text('{"run": "second"}')
            staged.report.write_text("# second report")
        assert report.read_text() == "# second report"
        assert ledger_path(report).read_text() == '{"run": "second"}'
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
        assert not (tmp_path / STAGING_DIR).exists()

    def test_a_rebuild_without_a_ledger_leaves_no_stale_ledger_live(self, tmp_path):
        report = _run(tmp_path, audit=False)
        with replacing(report, now=NOW) as staged:
            staged.report.write_text("# second report")  # the ledger write failed
        assert report.read_text() == "# second report"
        assert not ledger_path(report).exists()
        assert (tmp_path / "archive" / "AAPL_2026-09-26.210507.ledger.json").exists()

    def test_a_first_run_archives_nothing(self, tmp_path):
        report = tmp_path / "AAPL_2026-09-26.md"
        with replacing(report, now=NOW) as staged:
            staged.ledger.write_text("{}")
            staged.report.write_text("# first")
        assert staged.archived == [] and not (tmp_path / "archive").exists()
        assert report.read_text() == "# first" and ledger_path(report).exists()

    def test_two_rebuilds_within_a_second_archive_under_two_stamps(self, tmp_path):
        report = _run(tmp_path, audit=False)
        for n in ("second", "third"):
            with replacing(report, now=NOW) as staged:
                staged.report.write_text(f"# {n} report")
        arch = tmp_path / "archive"
        assert (arch / "AAPL_2026-09-26.210507.md").read_text() == "# first report"
        assert (arch / "AAPL_2026-09-26.210507-1.md").read_text() == "# second report"
        assert report.read_text() == "# third report"

    def test_staged_files_are_invisible_to_the_live_report_globs(self, tmp_path):
        report = _run(tmp_path, audit=False)
        with replacing(report, now=NOW) as staged:
            staged.report.write_text("# second report")
            live = [p.name for p in tmp_path.glob("AAPL_*.md") if is_live_report(p)]
            assert live == ["AAPL_2026-09-26.md"]

    def test_a_concurrent_rebuilds_staging_is_left_alone(self, tmp_path):
        report = _run(tmp_path, audit=False)
        other = tmp_path / STAGING_DIR / "someone-else.md"
        with replacing(report, now=NOW) as staged:
            other.write_text("# in progress")
            staged.report.write_text("# second report")
        assert other.read_text() == "# in progress"
