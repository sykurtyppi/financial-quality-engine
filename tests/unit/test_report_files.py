"""A report run is one generation, published whole or not at all.

- a same-day rerun keeps the earlier report (Hermes audit round 7, finding 3:
  rollback): earlier generations stay on disk and ``restore`` brings one back;
- a failed rebuild, or one without its ledger, changes nothing live (round 8,
  finding 2; deep audit, finding 2);
- two publishers never leave one's report beside the other's ledger (deep
  audit, finding 1), and neither does a publisher killed at any point (the
  re-audit, F1): the live names resolve through ONE pointer, swapped in one
  rename;
- a reader pins one generation (F3) and a lock or pointer it cannot read
  fails, never reads as "nothing published" (F2);
- a replay is never "the latest report".
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.reporting import report_files
from app.services.reporting.report_builder import ledger_path
from app.services.reporting.report_files import (
    ENGINE_ENV,
    ENGINE_LINE,
    GENERATION_LINE,
    GENERATIONS_DIR,
    STAGING_DIR,
    NotPublished,
    current_generation,
    engine_commit,
    generation_of,
    generations,
    is_live_report,
    read_live,
    replacing,
    restore,
    set_aside,
)

NOW = datetime(2026, 9, 26, 21, 5, 7, tzinfo=UTC)
NAME = "AAPL_2026-09-26.md"
ROOT = Path(__file__).resolve().parents[2]
ENGINE = "0123456789ab (stated by FQE_ENGINE_COMMIT; not a git checkout)"


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    """Publishes here state a fixed engine commit, not this checkout's."""
    monkeypatch.setenv(ENGINE_ENV, "0123456789ab")
    engine_commit.cache_clear()
    yield
    engine_commit.cache_clear()


def _stage(staged, tag, *, ledger=True, report=True):
    """What a rebuild writes: its report and its ledger."""
    if ledger:
        staged.ledger.write_text(json.dumps({"run": tag}))
    if report:
        staged.report.write_text(f"# {tag} report\n")


def _publish(dirpath, tag, name=NAME, now=NOW):
    report = dirpath / name
    with replacing(report, now=now) as staged:
        _stage(staged, tag)
    return report, staged


def _plain_run(dirpath, name=NAME, *, tag="first", audit=True):
    """Live files from before generations: plain files at the live names."""
    report = dirpath / name
    report.write_text(f"# {tag} report")
    ledger_path(report).write_text(f'{{"run": "{tag}"}}')
    if audit:
        report.with_name(f"{report.name.removesuffix('.md')}_audit.md").write_text(f"# {tag} audit")
    return report


def _leftovers(dirpath):
    """What rebuilds left in staging; the publish locks are meant to stay."""
    staging = dirpath / STAGING_DIR
    return [p for p in staging.iterdir() if p.suffix != ".lock"] if staging.is_dir() else []


def _live(dirpath, name=NAME):
    """The live names' contents, as a reader following them sees them."""
    report = dirpath / name
    return {p.name: p.read_bytes() for p in (
        report, ledger_path(report),
        report.with_name(f"{name.removesuffix('.md')}_audit.md")) if p.exists()}


def _one_generation(report):
    """The live report and ledger name the same generation."""
    gid = generation_of(report)
    return gid is not None and generation_of(ledger_path(report)) == gid


class TestIsLiveReport:
    def test_reports_are_live(self, tmp_path):
        assert is_live_report(tmp_path / NAME)

    def test_audits_and_replays_are_not(self, tmp_path):
        assert not is_live_report(tmp_path / "AAPL_2026-09-26_audit.md")
        assert not is_live_report(tmp_path / "AAPL_2025-06-30.replay.md")
        assert not is_live_report(tmp_path / "AAPL_2025-06-30.replay_audit.md")


class TestReplacing:
    def test_a_first_run_goes_live_through_the_pointer(self, tmp_path):
        report, staged = _publish(tmp_path, "first")
        gen = current_generation(report)
        assert gen is not None and gen.parent == tmp_path / GENERATIONS_DIR / "AAPL_2026-09-26"
        assert gen.name == f"20260926T210507Z_0001_{staged.generation_id}"
        assert report.is_symlink() and ledger_path(report).is_symlink()
        assert report.read_text().startswith("# first report")
        assert json.loads(ledger_path(report).read_text())["run"] == "first"
        assert _one_generation(report) and staged.archived == []
        # No audit yet: its live name is absent, not a link to nothing.
        assert not report.with_name("AAPL_2026-09-26_audit.md").is_symlink()
        assert not _leftovers(tmp_path)

    def test_a_rebuild_keeps_the_earlier_generation_whole(self, tmp_path):
        report, first = _publish(tmp_path, "first")
        earlier = current_generation(report)
        (earlier / "AAPL_2026-09-26_audit.md").write_text("# first audit")
        report2, second = _publish(tmp_path, "second")
        assert report.read_text().startswith("# second report") and _one_generation(report)
        assert sorted(p.name for p in second.archived) == [
            "AAPL_2026-09-26.ledger.json", "AAPL_2026-09-26.md", "AAPL_2026-09-26_audit.md"]
        assert all(p.parent == earlier for p in second.archived)
        assert (earlier / "AAPL_2026-09-26.md").read_text().startswith("# first report")
        # The earlier run's audit is not left at the live name.
        assert not report.with_name("AAPL_2026-09-26_audit.md").exists()
        assert [g.name for g in generations(report)] == [
            f"20260926T210507Z_0001_{first.generation_id}",
            f"20260926T210507Z_0002_{second.generation_id}"]

    def test_a_failed_rebuild_changes_nothing(self, tmp_path):
        report, _ = _publish(tmp_path, "first")
        before, kept = _live(tmp_path), generations(report)
        with pytest.raises(RuntimeError, match="build failed"):
            with replacing(report, now=NOW) as staged:
                staged.ledger.write_text('{"run": "half-built"}')
                raise RuntimeError("build failed")
        assert _live(tmp_path) == before and generations(report) == kept
        assert not _leftovers(tmp_path)

    def test_staged_files_are_invisible_to_the_live_report_globs(self, tmp_path):
        report, _ = _publish(tmp_path, "first")
        with replacing(report, now=NOW) as staged:
            _stage(staged, "second")
            live = [p.name for p in tmp_path.glob("AAPL_*.md") if is_live_report(p)]
            assert live == [NAME]

    def test_rebuilds_of_other_reports_share_staging_safely(self, tmp_path):
        """Round-9 audit F1: a rebuild committing must not remove the staging
        directory another is building in."""
        first = tmp_path / NAME
        other = tmp_path / "NVDA_2026-09-26.md"
        with replacing(other, now=NOW) as staged_other:  # mid-build, nothing written yet
            with replacing(first, now=NOW) as staged_first:
                _stage(staged_first, "AAPL")
            _stage(staged_other, "NVDA")
        assert first.read_text().startswith("# AAPL report")
        assert other.read_text().startswith("# NVDA report")

    def test_the_stamp_is_appended_to_the_builders_bytes(self, tmp_path):
        """`_seal` once read the report as text and wrote it back: CR and CRLF
        line ends became LF."""
        report = tmp_path / NAME
        with replacing(report, now=NOW) as staged:
            _stage(staged, "first")
            staged.report.write_bytes(b"line1\r\nline2\rline3\n")
        assert report.read_bytes().startswith(
            b"line1\r\nline2\rline3\n\n\n- Engine: " + ENGINE.encode() + b"\n- Generation: ")


class TestAWholeGenerationOrNothing:
    @pytest.mark.parametrize("missing, match", [("ledger", "no evidence ledger"),
                                                ("report", "wrote no report")])
    def test_a_rebuild_missing_a_file_publishes_nothing(self, tmp_path, missing, match):
        report, _ = _publish(tmp_path, "first")
        before, kept = _live(tmp_path), generations(report)
        with pytest.raises(NotPublished, match=match):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second", **{missing: False})
        assert _live(tmp_path) == before and generations(report) == kept
        assert not _leftovers(tmp_path)

    @pytest.mark.parametrize("stale", ["report", "ledger"])
    def test_a_file_naming_another_generation_publishes_nothing(self, tmp_path, stale):
        report, _ = _publish(tmp_path, "first")
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="names generation"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
                if stale == "report":
                    staged.report.write_text(f"# second\n{GENERATION_LINE}{'0' * 32}\n")
                else:
                    staged.ledger.write_text(json.dumps({"generation_id": "0" * 32}))
        assert _live(tmp_path) == before

    @pytest.mark.parametrize("bad", ["not json", "[1, 2]"])
    def test_a_ledger_that_is_not_a_json_object_publishes_nothing(self, tmp_path, bad):
        report, _ = _publish(tmp_path, "first")
        before = _live(tmp_path)
        with pytest.raises(NotPublished, match="evidence ledger is not"):
            with replacing(report, now=NOW) as staged:
                _stage(staged, "second")
                staged.ledger.write_text(bad)
        assert _live(tmp_path) == before

    def test_the_publish_stamps_the_report_and_ledger_with_one_generation(self, tmp_path):
        report, staged = _publish(tmp_path, "first")
        live = read_live(report)
        assert live is not None and live.generation_id == staged.generation_id
        assert live.text.endswith(
            f"{GENERATION_LINE}{staged.generation_id} (this report, its evidence ledger "
            "and its audit carry the same id)\n")
        assert json.loads(ledger_path(report).read_text()) == {
            "run": "first", "generation_id": staged.generation_id, "engine_commit": ENGINE}
        # The engine line sits directly above the generation line, which
        # stays the report's last.
        assert live.text.splitlines()[-2] == f"{ENGINE_LINE}{ENGINE}"


# --- the re-audit, F1: a publisher killed at any point ---------------------------------

_KILLED = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    sys.path.insert(0, {root!r})
    import app.services.reporting.report_files as rf

    stop, calls = int(sys.argv[1]), [0]

    def dies(real):
        def op(*a, **k):
            calls[0] += 1
            if calls[0] == stop:
                os._exit(77)  # no handler, no rollback: a SIGKILL or power loss
            return real(*a, **k)
        return op

    for name in ("replace", "rename", "symlink", "unlink"):
        setattr(rf.os, name, dies(getattr(os, name)))
    rf.Path.unlink = dies(rf.Path.unlink)
    with rf.replacing(Path({report!r})) as staged:
        staged.ledger.write_text(json.dumps({{"run": "second"}}))
        staged.report.write_text("# second report\\n")
""")


def test_a_publisher_killed_at_any_step_leaves_one_whole_generation_live(tmp_path):
    """Before: the ledger and the report were two replacements, and a process
    dying between them (os._exit, SIGKILL, power loss: no rollback runs) left
    the new ledger beside the old report. Now every step before the pointer's
    one rename leaves the old generation live, and every step after it the
    new one."""
    report, first = _publish(tmp_path, "first")
    seen = set()
    for stop in range(1, 60):
        proc = subprocess.run(
            [sys.executable, "-c", _KILLED.format(root=str(ROOT), report=str(report)),
             str(stop)], capture_output=True, text=True, timeout=60)
        assert proc.returncode in (0, 77), proc.stderr
        live = read_live(report)
        assert live is not None and _one_generation(report), (stop, live)
        assert live.ledger is not None and generation_of(live.ledger) == live.generation_id
        seen.add("new" if live.generation_id != first.generation_id else "old")
        # And the next publish succeeds from whatever the crash left.
        _publish(tmp_path, "again")
        assert _one_generation(report)
        restore(report, first.generation_id)  # back to the first run for the next kill
        if proc.returncode == 0:
            break
    else:
        pytest.fail("the publish never completed")
    assert seen == {"old", "new"}


# --- deep audit, finding 1: two publishers of one report --------------------------------


def _publisher(args):
    """One rebuild in its own process: stage, wait for the rival, publish.
    `os.replace` is slowed so an unlocked publish would interleave."""
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
    watcher on a filing night) interleaved and left report A beside ledger B."""
    import multiprocessing as mp

    report, _ = _publish(tmp_path, "first")
    ctx = mp.get_context("fork")
    with ctx.Manager() as manager:
        barrier = manager.Barrier(2)
        with ctx.Pool(2) as pool:
            ids = pool.map(_publisher, [(report, "A", barrier), (report, "B", barrier)])
    live = read_live(report)
    assert live is not None and live.generation_id in ids and _one_generation(report)
    assert len(generations(report)) == 3
    assert not _leftovers(tmp_path)


# --- Hermes audit of 424b0b4, finding 3a: a failure after the switch -------------------
# The publish swapped the pointer, then fsynced the directory and re-linked the
# audit. Either failing raised (the command said "failed", `NotPublished`'s
# handler said "the previous run stays live") with the NEW generation live. A
# raised error must mean the earlier generation is live.

AUDIT = "AAPL_2026-09-26_audit.md"


def _audited_first_run(tmp_path):
    """A live first run with its audit linked at the live name."""
    report, first = _publish(tmp_path, "first")
    (current_generation(report) / AUDIT).write_text(
        f"<!-- generation: {first.generation_id} -->\n\n# first audit")
    report_files.link_audit(report)
    return report, current_generation(report)


def _fail_after_the_switch(monkeypatch, report, *, exc=None):
    """The first ``_fsync`` once the pointer names a generation other than the
    one live now (None too: a set-aside) raises; ``hit`` records it."""
    import app.services.reporting.report_files as rf

    live, real, hit = current_generation(report), rf._fsync, []

    def fsync(path):
        if not hit and current_generation(report) != live:
            hit.append(current_generation(report))
            raise exc if exc is not None else OSError(errno.EIO, "injected: fsync after the switch")
        return real(path)

    monkeypatch.setattr(rf, "_fsync", fsync)
    return hit


def _set_apart_as_failed(report, gen, kept):
    """``gen``, switched back, is kept as ``.failed-<name>`` beside the runs
    (for diagnosis), and is not one of them."""
    assert not gen.exists()
    failed = gen.with_name(f".failed-{gen.name}")
    assert (failed / report.name).is_file() and generation_of(failed / report.name)
    assert generations(report) == kept


class TestAFailureAfterTheSwitch:
    def test_a_rebuild_is_switched_back_and_says_the_earlier_run_is_live(
            self, tmp_path, monkeypatch):
        report, earlier = _audited_first_run(tmp_path)
        before = _live(tmp_path)
        hit = _fail_after_the_switch(monkeypatch, report)
        with pytest.raises(NotPublished, match=f"{earlier.name} is live again"):
            _publish(tmp_path, "second")
        assert hit and hit[0] != earlier, "the failure was meant to follow the switch"
        assert current_generation(report) == earlier
        # Report, ledger and the earlier run's audit, byte for byte, at the live names.
        assert _live(tmp_path) == before and len(before) == 3
        assert not _leftovers(tmp_path)
        _set_apart_as_failed(report, hit[0], [earlier])

    def test_a_run_switched_back_holds_no_place_among_the_runs(self, tmp_path, monkeypatch):
        """Review of the 3a fix: the rolled-back generation stayed in
        ``.generations/<base>/`` as an ordinary run: listed, restorable by
        id, holding a sequence number. It is set apart, hidden, and the next
        publish takes the number it would have held."""
        report, earlier = _audited_first_run(tmp_path)
        hit = _fail_after_the_switch(monkeypatch, report)
        with pytest.raises(NotPublished):
            _publish(tmp_path, "second")
        failed_id = hit[0].name.rsplit("_", 1)[1]
        with pytest.raises(ValueError, match="0 generations match"):
            restore(report, failed_id)
        _publish(tmp_path, "third")  # the injection fires once
        assert [g.name.split("_")[1] for g in generations(report)] == ["0001", "0002"]

    def test_a_run_that_cannot_be_set_apart_still_says_why_the_publish_failed(
            self, tmp_path, monkeypatch):
        """Setting the failed run apart is tidying: if that rename fails too,
        the publish's own error is the one raised, not the rename's."""
        import app.services.reporting.report_files as rf

        report, earlier = _audited_first_run(tmp_path)
        hit = _fail_after_the_switch(monkeypatch, report)
        real = os.rename

        def rename(src, dst):
            if Path(dst).name.startswith(".failed-"):
                raise OSError(errno.EROFS, "injected: cannot set it apart")
            return real(src, dst)

        monkeypatch.setattr(rf.os, "rename", rename)
        with pytest.raises(NotPublished, match=f"{earlier.name} is live again"):
            _publish(tmp_path, "second")
        assert current_generation(report) == earlier and hit[0].is_dir()

    def test_a_first_publish_is_switched_back_to_nothing_live(self, tmp_path, monkeypatch):
        report = tmp_path / NAME
        hit = _fail_after_the_switch(monkeypatch, report)
        with pytest.raises(NotPublished, match="no run is live"):
            _publish(tmp_path, "first")
        assert hit
        assert current_generation(report) is None and read_live(report) is None
        assert not report.exists() and not ledger_path(report).exists()
        _set_apart_as_failed(report, hit[0], [])

    def test_an_interrupt_after_the_switch_is_raised_with_the_earlier_run_live(
            self, tmp_path, monkeypatch):
        """Not only an error: a Ctrl-C there must not leave the new run live
        either. It is raised as itself once the earlier run is back."""
        report, earlier = _audited_first_run(tmp_path)
        before = _live(tmp_path)
        hit = _fail_after_the_switch(monkeypatch, report, exc=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            _publish(tmp_path, "second")
        assert current_generation(report) == earlier and _live(tmp_path) == before
        _set_apart_as_failed(report, hit[0], [earlier])

    def test_a_switch_back_that_fails_says_the_new_run_may_be_live(self, tmp_path, monkeypatch):
        """Never "the earlier run is live" when putting it back failed."""
        import app.services.reporting.report_files as rf

        report, earlier = _audited_first_run(tmp_path)
        _fail_after_the_switch(monkeypatch, report)
        real = rf._symlink

        def symlink(link, target):
            if target == earlier.name:
                raise OSError(errno.EIO, "injected: cannot put the pointer back")
            return real(link, target)

        monkeypatch.setattr(rf, "_symlink", symlink)
        with pytest.raises(rf.PublishInDoubt, match="switching back failed .*cannot put the "
                           "pointer back.*: the NEW generation .* may be live") as e:
            _publish(tmp_path, "second")
        assert not isinstance(e.value, NotPublished)
        # ...and how to check, and how to put the earlier run back.
        assert f"readlink {rf._pointer(report)}" in str(e.value)
        assert f"restore({str(report)!r}, {earlier.name!r})" in str(e.value)
        assert current_generation(report) != earlier  # it is, in fact
        # ...so the new run stays where the pointer names it: never set apart.
        assert current_generation(report).is_dir() and _one_generation(report)
        assert current_generation(report) in generations(report)

    def test_a_switch_back_is_read_back_not_assumed(self, tmp_path, monkeypatch):
        """A put-back that raised nothing but did not take (the pointer still
        names the new run) is caught by reading the pointer back."""
        import app.services.reporting.report_files as rf

        report, earlier = _audited_first_run(tmp_path)
        _fail_after_the_switch(monkeypatch, report)
        real = rf._symlink
        monkeypatch.setattr(rf, "_symlink", lambda link, target: (
            None if target == earlier.name else real(link, target)))
        with pytest.raises(rf.PublishInDoubt, match="did not take .*: the NEW generation"):
            _publish(tmp_path, "second")

    def test_a_first_publish_whose_switch_back_fails_says_the_new_run_may_be_live(
            self, tmp_path, monkeypatch):
        import app.services.reporting.report_files as rf

        report = tmp_path / NAME
        _fail_after_the_switch(monkeypatch, report)
        real = rf.Path.unlink

        def unlink(self, *a, **k):
            if self.name == rf.CURRENT:
                raise OSError(errno.EIO, "injected: cannot remove the pointer")
            return real(self, *a, **k)

        monkeypatch.setattr(rf.Path, "unlink", unlink)
        with pytest.raises(rf.PublishInDoubt, match="may be live") as e:
            _publish(tmp_path, "first")
        # No earlier run to put back: taking the new one off is the way back.
        assert f"set_aside({str(report)!r})" in str(e.value)

    def test_the_earlier_runs_audit_is_gone_before_the_switch(self, tmp_path, monkeypatch):
        """The audit's live name resolves through the pointer: left in place
        across the switch it named the new run's audit, which does not exist.
        It is removed before the switch, the safe direction (the earlier run
        briefly shown without its audit, never beside another run's)."""
        import app.services.reporting.report_files as rf

        report, earlier = _audited_first_run(tmp_path)
        audit, real, seen = report.with_name(AUDIT), rf._symlink, []

        def symlink(link, target):
            if link.name == rf.CURRENT and target != earlier.name:
                seen.append(audit.is_symlink())
            return real(link, target)

        monkeypatch.setattr(rf, "_symlink", symlink)
        _publish(tmp_path, "second")
        assert seen == [False]
        assert not audit.is_symlink() and current_generation(report) != earlier

    def test_restore_is_switched_back(self, tmp_path, monkeypatch):
        report, earlier = _audited_first_run(tmp_path)
        _publish(tmp_path, "second")
        second = current_generation(report)
        before = _live(tmp_path)
        _fail_after_the_switch(monkeypatch, report)
        with pytest.raises(NotPublished, match=f"{second.name} is live again"):
            restore(report, earlier.name)
        assert current_generation(report) == second and _live(tmp_path) == before

    def test_a_restore_relinks_the_restored_runs_audit(self, tmp_path):
        report, earlier = _audited_first_run(tmp_path)
        _publish(tmp_path, "second")
        assert not report.with_name(AUDIT).exists()
        restore(report, earlier.name)
        assert report.with_name(AUDIT).read_text().endswith("# first audit")

    def test_a_set_aside_whose_switch_back_fails_says_it_may_be_set_aside(
            self, tmp_path, monkeypatch):
        import app.services.reporting.report_files as rf

        report, earlier = _audited_first_run(tmp_path)
        _fail_after_the_switch(monkeypatch, report)
        real = rf._symlink
        monkeypatch.setattr(rf, "_symlink", lambda link, target: (
            None if target == earlier.name else real(link, target)))
        with pytest.raises(rf.PublishInDoubt, match="setting it aside failed .* the run may "
                           "be set aside, with no run live") as e:
            set_aside(report)
        assert current_generation(report) is None
        assert f"restore({str(report)!r}, {earlier.name!r})" in str(e.value)

    def test_set_aside_is_switched_back(self, tmp_path, monkeypatch):
        report, earlier = _audited_first_run(tmp_path)
        before = _live(tmp_path)
        hit = _fail_after_the_switch(monkeypatch, report)
        with pytest.raises(NotPublished, match=f"{earlier.name} is live again"):
            set_aside(report)
        assert hit == [None]
        assert current_generation(report) == earlier and _live(tmp_path) == before


_OS_OPS = ("open", "fsync", "rename", "replace", "symlink", "unlink", "readlink", "chmod")


def _failing_at(stop):
    """A wrapper for filesystem calls whose ``stop``-th call, while
    ``state["armed"]``, raises EIO."""
    state = {"calls": 0, "armed": True}

    def wrap(real):
        def op(*a, **k):
            if state["armed"]:
                state["calls"] += 1
                if state["calls"] == stop:
                    raise OSError(errno.EIO, f"injected at call {stop}")
            return real(*a, **k)
        return op
    return state, wrap


def test_a_publish_that_raises_at_any_step_leaves_the_earlier_run_live(tmp_path, monkeypatch):
    """Every filesystem call a publish makes, failed in turn: whatever raised,
    the earlier run is live, byte for byte (its audit too). The kill test
    above covers a process that dies; this one a process that lives on to
    say it failed."""
    report, earlier = _audited_first_run(tmp_path)
    before = _live(tmp_path)
    for stop in range(1, 400):
        state, failing = _failing_at(stop)
        with monkeypatch.context() as m:
            for name in _OS_OPS:
                m.setattr(os, name, failing(getattr(os, name)))
            try:
                _publish(tmp_path, f"try {stop}")
            except Exception:  # noqa: BLE001 - any failure at all
                state["armed"] = False
                assert current_generation(report) == earlier, stop
                assert _live(tmp_path) == before, stop
                # ...and the run that failed is not kept as one (review of the 3a fix).
                assert generations(report) == [earlier], stop
                continue
            finally:
                state["armed"] = False
        assert current_generation(report) != earlier and _one_generation(report)
        break
    else:
        pytest.fail("the publish never completed")
    assert stop > 10, "the injection never reached the publish"


# --- the re-audit, F2 and F3: readers --------------------------------------------------


class TestReadLive:
    def test_a_reader_is_pinned_to_one_generation(self, tmp_path):
        """F3: a reader that returned live NAMES read them later, after a
        rebuild could have replaced them. The paths it returns are the
        generation's own, which no later publish changes."""
        report, first = _publish(tmp_path, "first")
        live = read_live(report)
        _publish(tmp_path, "second")
        assert live.report.parent.name.endswith(first.generation_id)
        assert live.report.read_text() == live.text
        assert live.audit is None and live.stale == ()  # no audit is not a stale one
        assert json.loads(live.ledger.read_text())["run"] == "first"

    def test_an_audit_of_another_generation_is_stale_not_the_reports(self, tmp_path):
        report, staged = _publish(tmp_path, "first")
        audit = current_generation(report) / "AAPL_2026-09-26_audit.md"
        audit.write_text(f"<!-- generation: {'f' * 32} -->\n\n# someone else's audit")
        live = read_live(report)
        assert live.audit is None and live.stale == (audit,)
        audit.write_text(f"<!-- generation: {staged.generation_id} -->\n\n# its audit")
        assert read_live(report).audit == audit

    def test_files_from_before_generations_still_pair(self, tmp_path):
        report = _plain_run(tmp_path, audit=False)
        live = read_live(report)
        assert live is not None and live.generation_id is None and live.generation_dir is None
        assert live.ledger == ledger_path(report) and not live.stale
        # No audit: none returned, though a missing file also names no generation.
        assert live.audit is None

    def test_no_report_reads_as_none(self, tmp_path):
        assert read_live(tmp_path / NAME) is None

    def test_reading_creates_nothing(self, tmp_path):
        report, _ = _publish(tmp_path, "first")
        arch = current_generation(report)
        before = sorted(p.name for p in arch.iterdir())
        assert read_live(arch / NAME).text.startswith("# first report")
        assert sorted(p.name for p in arch.iterdir()) == before
        assert read_live(tmp_path / "nowhere" / NAME) is None
        assert not (tmp_path / "nowhere").exists()

    def test_a_pointer_that_cannot_be_read_fails_closed(self, tmp_path, monkeypatch):
        """F2: any failure to open the reader's lock was read as "nothing
        published" and the reader read unlocked, mid-publish. Only a missing
        pointer means none; anything else raises."""
        import app.services.reporting.report_files as rf

        report, _ = _publish(tmp_path, "first")

        def denied(path):
            raise PermissionError("not ours")

        monkeypatch.setattr(rf.os, "readlink", denied)
        with pytest.raises(PermissionError):
            read_live(report)

    def test_a_publisher_that_cannot_take_the_lock_publishes_nothing(self, tmp_path, monkeypatch):
        import app.services.reporting.report_files as rf

        report, _ = _publish(tmp_path, "first")
        before = _live(tmp_path)
        real_open = rf.os.open

        def no_lock(path, *a, **k):
            if str(path).endswith(".lock"):
                raise PermissionError("read-only")
            return real_open(path, *a, **k)

        monkeypatch.setattr(rf.os, "open", no_lock)
        with pytest.raises(PermissionError, match="read-only"):
            _publish(tmp_path, "second")
        assert _live(tmp_path) == before


# --- set aside, restore, and files from before generations -----------------------------


class TestSetAsideAndRestore:
    def test_set_aside_takes_the_run_off_the_live_names_and_keeps_it(self, tmp_path):
        report, staged = _publish(tmp_path, "first")
        gen = current_generation(report)
        aside = set_aside(report)
        assert sorted(p.name for p in aside) == ["AAPL_2026-09-26.ledger.json", NAME]
        assert read_live(report) is None and not report.exists()
        assert (gen / NAME).read_text().startswith("# first report")
        assert [p.name for p in tmp_path.glob("AAPL_*.md") if p.exists()] == []

    def test_restore_brings_a_kept_generation_back_in_one_step(self, tmp_path):
        report, first = _publish(tmp_path, "first")
        _publish(tmp_path, "second")
        assert restore(report, first.generation_id).parent == current_generation(report)
        assert report.read_text().startswith("# first report") and _one_generation(report)

    @pytest.mark.parametrize("which", ["nope", "20260926T210507Z"])
    def test_restore_refuses_an_unknown_or_ambiguous_generation(self, tmp_path, which):
        report, _ = _publish(tmp_path, "first")
        _publish(tmp_path, "second")  # same stamp: the stamp alone is ambiguous
        live = current_generation(report)
        with pytest.raises(ValueError, match="generations match"):
            restore(report, which)
        assert current_generation(report) == live

    def test_nothing_published_sets_nothing_aside(self, tmp_path):
        assert set_aside(tmp_path / NAME) == []
        assert not (tmp_path / GENERATIONS_DIR).exists()

    def test_a_run_from_before_generations_is_kept_by_the_first_rebuild(self, tmp_path):
        report = _plain_run(tmp_path)
        _, staged = _publish(tmp_path, "second")
        assert report.read_text().startswith("# second report") and _one_generation(report)
        adopted = {p.name: p.read_text() for p in staged.archived}
        assert adopted == {NAME: "# first report", "AAPL_2026-09-26.ledger.json": '{"run": "first"}',
                           "AAPL_2026-09-26_audit.md": "# first audit"}
        assert not report.with_name("AAPL_2026-09-26_audit.md").exists()

    def test_a_plain_file_that_is_not_the_live_runs_stops_the_rebuild(self, tmp_path):
        report, _ = _publish(tmp_path, "first")
        ledger = ledger_path(report)
        ledger.unlink()
        ledger.write_text('{"run": "copied back by hand"}')
        with pytest.raises(NotPublished, match="set it aside by hand"):
            _publish(tmp_path, "second")
        assert ledger.read_text() == '{"run": "copied back by hand"}'

    def test_a_replay_keeps_its_replay_names(self, tmp_path):
        report, _ = _publish(tmp_path, "replay", name="AAPL_2025-06-30.replay.md")
        assert ledger_path(report).name == "AAPL_2025-06-30.replay.ledger.json"
        assert current_generation(report).parent.name == "AAPL_2025-06-30.replay"
        assert _one_generation(report)


# --- review of #100 --------------------------------------------------------------------


class TestReviewOfTheGenerationLayout:
    def test_restore_does_not_overwrite_a_hand_placed_file(self, tmp_path):
        """A rebuild and set_aside refused a plain file that is not the live
        run's; restore replaced it with a link, and the operator's copy was
        gone."""
        report, first = _publish(tmp_path, "first")
        _publish(tmp_path, "second")
        report.unlink()
        report.write_text("# the operator's copy")
        with pytest.raises(NotPublished, match="set it aside by hand"):
            restore(report, first.generation_id)
        assert report.read_text() == "# the operator's copy" and not report.is_symlink()

    def test_a_failed_copy_of_files_from_before_generations_leaves_nothing(
            self, tmp_path, monkeypatch):
        """The copy into a new generation left a hidden partial directory
        that `generations()` listed and `restore` could make live."""
        import app.services.reporting.report_files as rf

        report = _plain_run(tmp_path)
        real = rf.shutil.copy2

        def full(src, dst, *a, **k):
            if str(dst).endswith(".ledger.json"):
                raise OSError("ENOSPC")
            return real(src, dst, *a, **k)

        monkeypatch.setattr(rf.shutil, "copy2", full)
        with pytest.raises(OSError, match="ENOSPC"):
            _publish(tmp_path, "second")
        monkeypatch.undo()
        home = tmp_path / GENERATIONS_DIR / "AAPL_2026-09-26"
        assert [p.name for p in home.iterdir()] == [] and generations(report) == []
        assert report.read_text() == "# first report" and not report.is_symlink()

    def test_hidden_empty_and_pointer_directories_are_not_generations(self, tmp_path):
        report, first = _publish(tmp_path, "first")
        home = current_generation(report).parent
        (home / f".{current_generation(report).name}.1234abcd").mkdir()  # an interrupted copy
        (home / "20260926T210507Z_0009_empty").mkdir()  # holds no report
        assert [g.name for g in generations(report)] == [current_generation(report).name]
        # An exact name is not ambiguous with a hidden leftover containing it.
        assert restore(report, current_generation(report).name).parent == current_generation(report)

    def test_a_crash_after_copying_old_files_is_finished_not_repeated(self, tmp_path):
        """A crash after the copy of pre-generation files but before the
        pointer: the next rebuild raised a raw OSError (same second) or
        copied them again (a later second)."""
        import app.services.reporting.report_files as rf

        report = _plain_run(tmp_path)
        (tmp_path / GENERATIONS_DIR / "AAPL_2026-09-26").mkdir(parents=True)
        plain = {r: p for r, p in rf._companions(report).items() if p.exists()}
        rf._copy_adopted(report, plain)  # then the process died
        _publish(tmp_path, "second")
        _publish(tmp_path, "third")
        adopted = [g for g in generations(report) if g.name.endswith("_adopted")]
        assert len(adopted) == 1 and (adopted[0] / NAME).read_text() == "# first report"
        assert restore(report, "adopted") == adopted[0] / NAME

    def test_generations_are_listed_in_publish_order_within_one_second(self, tmp_path):
        """Named `<stamp>_<id>`, same-second generations sorted by their
        random id, and the first rebuild after this change always published
        the kept run and the new one in the same second."""
        report = _plain_run(tmp_path)
        tags = ["second", "third", "fourth", "fifth"]
        for tag in tags:
            _publish(tmp_path, tag)
        texts = [(g / NAME).read_text().split("\n")[0] for g in generations(report)]
        assert texts == ["# first report"] + [f"# {t} report" for t in tags]

    def test_a_copy_with_links_dereferenced_says_so(self, tmp_path):
        """`shutil.copytree` (or `cp -L`) turns the pointer into a directory;
        every call then failed with a bare EINVAL."""
        import shutil

        src = tmp_path / "reports"
        src.mkdir()
        _publish(src, "first")
        dst = tmp_path / "copied"
        shutil.copytree(src, dst)
        with pytest.raises(OSError, match="copied with its links dereferenced"):
            read_live(dst / NAME)
        assert [g.name for g in generations(dst / NAME)] == [
            g.name for g in generations(src / NAME)]

    def test_live_name_maps_only_a_generations_own_path(self, tmp_path):
        from app.services.reporting.report_files import live_name

        report, _ = _publish(tmp_path, "first")
        gen = current_generation(report)
        assert live_name(gen / NAME) == report and live_name(report) == report
        # Not under `.generations`, or under another report's: not a generation.
        lookalike = tmp_path / "kept" / "AAPL_2026-09-26" / "x" / NAME
        other = tmp_path / GENERATIONS_DIR / "NVDA_2026-09-26" / "x" / NAME
        assert live_name(lookalike) == lookalike and live_name(other) == other

    def test_reading_a_generations_own_path_reads_that_generation(self, tmp_path):
        report, first = _publish(tmp_path, "first")
        gen = current_generation(report)
        _publish(tmp_path, "second")
        live = read_live(gen / NAME)
        assert live.generation_id == first.generation_id and live.generation_dir == gen
        assert json.loads(live.ledger.read_text())["run"] == "first"

    def test_only_a_generation_name_has_a_sequence_number(self):
        import app.services.reporting.report_files as rf

        assert rf._seq(Path("20260926T210507Z_0003_abc")) == 3
        assert rf._seq(Path("20260926T210507Z_0003")) == -1  # no id: not one of ours
        assert rf._seq(Path("current")) == -1

    def test_a_kept_pre_generation_run_is_stamped_with_its_newest_file(self, tmp_path):
        """Its files were written at different times; the run is as old as
        the last of them, not the first."""
        report = _plain_run(tmp_path, audit=False)
        os.utime(report, (1_700_000_000, 1_700_000_000))  # 2023-11-14T22:13:20Z
        os.utime(ledger_path(report), (1_800_000_000, 1_800_000_000))  # 2027-01-15T08:00:00Z
        _publish(tmp_path, "second")
        (adopted,) = [g for g in generations(report) if g.name.endswith("_adopted")]
        assert adopted.name.startswith("20270115T080000Z_")

    def test_published_files_are_read_only(self, tmp_path):
        """A write to a live name (a hand `cp` over it) went through the link
        into the kept generation. Published files are read-only."""
        import stat

        report, _ = _publish(tmp_path, "first")
        gen = current_generation(report)
        for name in (NAME, "AAPL_2026-09-26.ledger.json"):
            assert stat.S_IMODE((gen / name).stat().st_mode) == 0o444


def test_a_ledger_failure_is_logged_under_its_report_and_raised(tmp_path, monkeypatch, caplog):
    """R4: built into staging, a failed ledger logged only its staging name,
    and the operator could not tell which report. It is also raised: the run
    is not published without it."""
    from datetime import date

    from app.services.reporting import report_builder

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", False)
    with caplog.at_level("ERROR"), pytest.raises(NotPublished, match="AAPL 2026-09-26"):
        report_builder.write_ledger(
            tmp_path / ".staging" / "0123456789ab.ledger.json",
            ticker="AAPL", report_date=date(2026, 9, 26), result=None)
    assert "AAPL 2026-09-26" in caplog.text


class TestEngineCommit:
    """A season runs from a pinned commit; every published run names it, and
    says so when the code it ran is not that commit."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        root = tmp_path / "engine"
        (root / "app").mkdir(parents=True)
        (root / "journal").mkdir()
        (root / "app" / "mod.py").write_text("X = 1\n")
        (root / "journal" / "watchlist.json").write_text("{}\n")
        (root / ".gitignore").write_text("__pycache__/\n")

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=root, check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "engine")
        monkeypatch.setattr(report_files, "_ENGINE_ROOT", root)
        engine_commit.cache_clear()
        return root, git("rev-parse", "--short=12", "HEAD")

    def test_a_clean_checkout_names_its_commit(self, repo):
        _, sha = repo
        assert engine_commit() == f"{sha} (clean checkout)"

    @pytest.mark.parametrize("change", ["edit", "untracked"])
    def test_changed_engine_code_is_named_as_not_that_commit(self, repo, change):
        root, sha = repo
        if change == "edit":
            (root / "app" / "mod.py").write_text("X = 2\n")
        else:
            (root / "app" / "new.py").write_text("Y = 1\n")
        assert engine_commit() == (
            f"{sha} + uncommitted changes to the engine code (not reproducible from {sha})")

    def test_data_the_engine_rewrites_and_caches_do_not_count(self, repo):
        """`watch.py` rewrites journal/watchlist.json on every re-arm."""
        root, sha = repo
        (root / "journal" / "watchlist.json").write_text('{"re": "armed"}\n')
        (root / "app" / "__pycache__").mkdir()
        (root / "app" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
        assert engine_commit() == f"{sha} (clean checkout)"

    def test_read_once_per_process(self, repo):
        """The code a running process loaded is the code it started with."""
        root, sha = repo
        assert engine_commit() == f"{sha} (clean checkout)"
        (root / "app" / "mod.py").write_text("X = 2\n")
        assert engine_commit() == f"{sha} (clean checkout)"

    def test_not_a_checkout(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        monkeypatch.setattr(report_files, "_ENGINE_ROOT", tmp_path)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        engine_commit.cache_clear()
        assert engine_commit() == "unknown (not run from a git checkout; set FQE_ENGINE_COMMIT)"

    def test_a_copy_inside_another_checkout_is_not_that_checkout(self, repo, monkeypatch):
        """An installed copy (site-packages under a venv inside some repo)
        must not report the enclosing repo's commit."""
        root, _ = repo
        inner = root / "venv" / "lib"
        inner.mkdir(parents=True)
        monkeypatch.setattr(report_files, "_ENGINE_ROOT", inner)
        assert engine_commit() == "unknown (not run from a git checkout; set FQE_ENGINE_COMMIT)"

    def test_no_git_binary(self, repo, monkeypatch):
        def missing(*a, **k):
            raise FileNotFoundError("git")
        monkeypatch.setattr(report_files.subprocess, "run", missing)
        assert engine_commit().startswith("unknown (not run from a git checkout")

    def test_status_that_fails_is_said_not_read_as_clean(self, repo, monkeypatch):
        _, sha = repo
        real = subprocess.run

        def status_fails(argv, *a, **k):
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "fatal")
            return real(argv, *a, **k)
        monkeypatch.setattr(report_files.subprocess, "run", status_fails)
        assert engine_commit() == (
            f"{sha} (could not check the checkout for uncommitted changes)")

    def test_a_stated_commit_says_it_was_stated(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV, "  c62095d  ")
        engine_commit.cache_clear()
        assert engine_commit() == "c62095d (stated by FQE_ENGINE_COMMIT; not a git checkout)"

    def test_a_blank_statement_is_no_statement(self, repo, monkeypatch):
        _, sha = repo
        monkeypatch.setenv(ENGINE_ENV, "   ")
        engine_commit.cache_clear()
        assert engine_commit() == f"{sha} (clean checkout)"


class TestEngineCommitNeverBlocksAPublish:
    """Review of #102: the stamp is metadata about the run, so reading it can
    never be the reason a finished rebuild is not published."""

    def test_a_path_git_prints_undecodably_is_read_not_raised(self, tmp_path, monkeypatch):
        """With core.quotePath=false git prints a Latin-1 file name raw;
        decoding it as UTF-8 raised UnicodeDecodeError out of every publish."""
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        root = tmp_path / "engine"
        (root / "app").mkdir(parents=True)
        (root / "app" / "mod.py").write_text("X = 1\n")

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            *args], cwd=root, check=True, capture_output=True)

        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "engine")
        git("config", "core.quotePath", "false")
        try:
            (root / "app" / os.fsdecode(b"caf\xe9.py")).write_text("Y = 1\n")
        except OSError as e:
            if e.errno != errno.EILSEQ:
                raise
            # macOS (APFS) refuses a name that is not valid UTF-8, so no real
            # checkout there can hold one; the portable test below drives the
            # same decoding path with a fake git.
            pytest.skip("this filesystem refuses non-UTF-8 file names (EILSEQ)")
        monkeypatch.setattr(report_files, "_ENGINE_ROOT", root)
        engine_commit.cache_clear()
        assert "uncommitted changes" in engine_commit()

    def test_undecodable_git_output_is_read_not_raised_on_any_platform(
            self, tmp_path, monkeypatch):
        """The same defect as above, on every platform: git's own bytes are
        what `_git` decodes, so a fake git on PATH prints a Latin-1 name raw
        (Hermes: the real-file version cannot run on macOS, whose filesystem
        rejects the name before the code under test is reached)."""
        monkeypatch.delenv(ENGINE_ENV, raising=False)
        root = tmp_path / "engine"
        root.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "git"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$*" in
              *--show-toplevel*) printf '%s\\n' '{root}' ;;
              *rev-parse*) printf 'abcdef123456\\n' ;;
              *status*) printf '?? app/caf\\351.py\\n' ;;
              *) exit 1 ;;
            esac
            """))
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setattr(report_files, "_ENGINE_ROOT", root)
        engine_commit.cache_clear()
        stamp = engine_commit()
        assert stamp == ("abcdef123456 + uncommitted changes to the engine code "
                         "(not reproducible from abcdef123456)")

    def test_a_stamp_that_cannot_be_read_still_publishes(self, tmp_path, monkeypatch):
        def broken():
            raise RuntimeError("no stamp today")
        monkeypatch.setattr(report_files, "engine_commit", broken)
        report, staged = _publish(tmp_path, "first")
        live = read_live(report)
        assert live is not None and live.generation_id == staged.generation_id
        line = live.text.splitlines()[-2]
        assert line.startswith(f"{ENGINE_LINE}unknown (")
        assert "RuntimeError: no stamp today" in line
        assert json.loads(ledger_path(report).read_text())["engine_commit"] == line[len(ENGINE_LINE):]

    def test_the_commit_is_read_when_the_module_loads_not_at_the_first_publish(self):
        """A long-lived process (the web UI) that publishes after a `git pull`
        must name the code it loaded, not the checkout's new HEAD."""
        probe = (
            "from app.services.reporting import report_files as r; "
            "print(r.engine_commit.cache_info().currsize)"
        )
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True,
                             text=True, timeout=60, env={**os.environ, ENGINE_ENV: "abc1234"})
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "1"


def test_generate_report_says_a_publish_in_doubt_with_its_own_exit_code(monkeypatch, capsys):
    """Follow-up to finding 3a: `PublishInDoubt` is not a `NotPublished`, so
    `generate_report.py` let it out as a traceback (exit 1)."""
    import importlib.util

    from app.services.reporting.report_files import PUBLISH_IN_DOUBT_RC, PublishInDoubt

    spec = importlib.util.spec_from_file_location("generate_report_cli",
                                                  ROOT / "scripts" / "generate_report.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def main():
        raise PublishInDoubt("AAPL_x.md: publishing g failed, and switching back failed: "
                             "the NEW generation g may be live.")

    monkeypatch.setattr(cli, "main", main)
    assert cli._main() == PUBLISH_IN_DOUBT_RC == 8
    err = capsys.readouterr().err
    assert "the NEW generation g may be live" in err
    assert "no report published" not in err  # that is NotPublished's line, not this one's



# --- review of 68dbc24, L-2: a publish lock that a reader waits for only so long ------


def _holder(lock: Path):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys\n"
         f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "sys.stdin.read()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_a_publish_lock_with_a_timeout_gives_up_on_a_holder(tmp_path):
    import time

    report = tmp_path / NAME
    (tmp_path / STAGING_DIR).mkdir()
    holder = _holder(tmp_path / STAGING_DIR / "AAPL_2026-09-26.lock")
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="publish"):
            with report_files.publish_lock(report, timeout=0.3):
                pass
        waited = time.monotonic() - t0
    finally:
        holder.communicate("")
    assert 0.25 < waited < 5
    with report_files.publish_lock(report, timeout=0.3):  # free: taken at once
        pass


def test_a_publish_lock_without_a_timeout_waits_for_its_holder(tmp_path):
    import threading
    import time

    report = tmp_path / NAME
    (tmp_path / STAGING_DIR).mkdir()
    holder = _holder(tmp_path / STAGING_DIR / "AAPL_2026-09-26.lock")
    got = []

    def take():
        with report_files.publish_lock(report):
            got.append(time.monotonic())

    t = threading.Thread(target=take)
    t.start()
    t.join(0.6)
    assert got == []                        # still waiting: publishers keep blocking
    released = time.monotonic()
    holder.communicate("")
    t.join(10)
    assert got and got[0] >= released



def test_a_publish_lock_timeout_of_zero_tries_once_and_never_sleeps(tmp_path, monkeypatch):
    """Review of eeb1e51, L-3: a timeout is a deadline, and a deadline
    already reached gives up after the one try, however the clock reads."""
    import fcntl

    report = tmp_path / NAME
    (tmp_path / STAGING_DIR).mkdir()
    lock = tmp_path / STAGING_DIR / "AAPL_2026-09-26.lock"
    held = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    slept = []

    class Frozen:
        @staticmethod
        def monotonic():
            return 1000.0

        @staticmethod
        def sleep(seconds):
            slept.append(seconds)
            raise AssertionError("slept past a deadline already reached")

    monkeypatch.setattr(report_files, "time", Frozen)
    try:
        with pytest.raises(TimeoutError):
            with report_files.publish_lock(report, timeout=0):
                pass
    finally:
        os.close(held)
    assert slept == []
