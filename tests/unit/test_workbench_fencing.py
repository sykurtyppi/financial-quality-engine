"""Publication fencing: a superseded run never becomes the live one.

Hermes audit of PR #118 @ 3983f8a (finding 1, blocker): a workbench run A
stalls, the operator starts run B, B publishes, and A — which a thread
cannot be stopped from finishing — publishes after it and becomes the live
card while the page says B is done. Ignoring A's result in the job registry
did not help: `report_files.replacing` had already switched ``current``
before the build returned. And two processes (two servers on one reports
folder) raced the same way with no registry in common.

The fix is on disk, so it holds across processes: each run is given the
next number of the ticker's request counter (``reports/workbench/.epochs/<T>``)
when it is asked for, the number is sealed into its generation (the
ledger's ``fence``), and the publish compares it, under the publish lock,
with the live generation's before switching: a lower one is kept in the
history and never made live (`Superseded`). These read what is live
(`read_live`), not the job registry.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.ledger import LedgerDocument
from app.services.journal import reporting
from app.services.reporting import report_files
from app.services.reporting.report_files import (
    Superseded,
    current_generation,
    generations,
    read_count,
    read_live,
    replacing,
)
from app.services.workbench import fencing, jobs

NAME = "KO_2026-10-09.md"


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    return tmp_path / "reports"


def _fence(report: Path, number: int) -> report_files.Fence:
    """Request ``number`` of KO, with the ticker's high-water mark and lock
    beside ``report`` as `fencing.fence` places them in reports/workbench."""
    epochs = report.parent / fencing.EPOCHS_DIR
    return report_files.Fence(number, epochs / "KO.published", epochs / "KO.lock")


def _publish(report: Path, tag: str, fence: int | report_files.Fence | None = None,
             **kw) -> str:
    """One rebuild of ``report``, fenced when ``fence`` is given (as the
    workbench's runs are) and not otherwise (the journal, the auto track,
    the CLI). Returns its generation id."""
    if isinstance(fence, int):
        fence = _fence(report, fence)
    extra = {} if fence is None else {"fence": fence}
    with replacing(report, **extra, **kw) as staged:
        staged.report.write_text(f"# {tag} report\n")
        staged.ledger.write_text(json.dumps({"run": tag}))
    return staged.generation_id


def _ledger(report: Path) -> dict:
    live = read_live(report)
    assert live is not None and live.ledger is not None
    return json.loads(live.ledger.read_text())


# --- the publish: fenced, compared with the live generation's fence ---------------------


def test_a_lower_fence_is_kept_in_the_history_and_never_made_live(tmp_path):
    report = tmp_path / NAME
    newer = _publish(report, "B", fence=2)
    with pytest.raises(Superseded) as e:
        _publish(report, "A", fence=1)
    live = read_live(report)
    assert live is not None and live.generation_id == newer and "B report" in live.text
    # A is kept, whole, as a run of the history: not live, not set apart.
    kept = [g for g in generations(report) if g.name.endswith(e.value.generation_id)]
    assert len(kept) == 1 and kept[0] != current_generation(report)
    assert (kept[0] / NAME).read_text().startswith("# A report")
    assert json.loads((kept[0] / "KO_2026-10-09.ledger.json").read_text())["fence"] == 1
    assert e.value.fence == 1 and e.value.live_fence == 2
    assert e.value.live_generation_id == newer
    assert newer in str(e.value) and "superseded" in str(e.value)
    # Not a failure of the publish: nothing is "not published" about it.
    assert not isinstance(e.value, report_files.NotPublished)
    assert not list((tmp_path / report_files.STAGING_DIR).glob("[0-9a-f]*"))


@pytest.mark.parametrize("first,second", [(1, 1), (1, 2), (3, 40)])
def test_an_equal_or_higher_fence_publishes(tmp_path, first, second):
    report = tmp_path / NAME
    _publish(report, "A", fence=first)
    gid = _publish(report, "B", fence=second)
    assert read_live(report).generation_id == gid
    assert _ledger(report)["fence"] == second


def test_a_negative_fence_is_refused_and_publishes_nothing(tmp_path):
    """A request number is 1 or more (`fencing.request`); a fence below 0
    is a caller's error, never sealed."""
    report = tmp_path / NAME
    with pytest.raises(ValueError, match="fence"):
        _publish(report, "A", fence=-1)
    assert read_live(report) is None and generations(report) == []
    gid = _publish(report, "B", fence=0)
    assert read_live(report).generation_id == gid


def test_a_fence_publishes_over_a_live_run_without_one(tmp_path):
    """A live run from before fences (or from another track) holds no
    fence: nothing to be superseded by."""
    report = tmp_path / NAME
    _publish(report, "old")
    gid = _publish(report, "new", fence=1)
    assert read_live(report).generation_id == gid


def test_without_a_fence_a_publish_is_as_before_even_over_a_fenced_live_run(tmp_path):
    """The journal track, the auto track and the CLI pass no fence: their
    publish is what it was (the newest publish is live), whatever fence the
    live run carries, and their ledger gains no field."""
    report = tmp_path / NAME
    _publish(report, "fenced", fence=7)
    gid = _publish(report, "plain")
    assert read_live(report).generation_id == gid
    assert "fence" not in _ledger(report)


def test_a_live_ledger_that_states_no_usable_fence_is_no_fence(tmp_path):
    """A live generation whose ledger names no whole-number fence (an
    adopted run, a ledger from before fences, a hand edit) holds nothing a
    run is superseded by: refusing every run after it would leave the
    ticker stuck on it."""
    report = tmp_path / NAME
    # Published with fence 0, the lowest a run can carry, so any of these
    # read as a number would supersede it.
    for bad in ('{"fence": true}', '{"fence": "9"}', '{"fence": -1}', "not json",
                '{"fence": 9.5}', "[]", '{"fence": null}', "{}"):
        with replacing(report) as staged:
            staged.report.write_text("# old report\n")
            staged.ledger.write_text("{}")
        gen = current_generation(report)
        ledger = gen / "KO_2026-10-09.ledger.json"
        ledger.chmod(0o644)
        ledger.write_text(bad)
        gid = _publish(report, "new", fence=0)
        assert read_live(report).generation_id == gid, bad


def test_a_live_ledger_that_is_a_symlink_is_not_followed_for_its_fence(tmp_path):
    """A link planted in the live generation could name any file with a high
    fence and refuse every run after it: it is not read."""
    report = tmp_path / NAME
    _publish(report, "old", fence=1)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"fence": 999}')
    gen = current_generation(report)
    gen.chmod(0o755)
    ledger = gen / "KO_2026-10-09.ledger.json"
    ledger.unlink()
    ledger.symlink_to(elsewhere)
    gid = _publish(report, "new", fence=2)
    assert read_live(report).generation_id == gid


def test_the_fence_round_trips_through_the_ledger_model(tmp_path):
    report = tmp_path / NAME
    with replacing(report, fence=_fence(report, 12)) as staged:
        staged.report.write_text("# r\n")
        staged.ledger.write_text(LedgerDocument(
            ticker="KO", generated_on=date(2026, 10, 9), config_version="0.3.0",
        ).model_dump_json(indent=1, exclude={"fence"}))
    doc = LedgerDocument.model_validate_json(read_live(report).ledger.read_text())
    assert doc.fence == 12 and doc.generation_id == staged.generation_id
    assert LedgerDocument.model_validate_json(doc.model_dump_json()).fence == 12
    # A ledger from before fences loads, with none.
    old = LedgerDocument.model_validate_json(json.dumps(
        {"ticker": "KO", "generated_on": "2026-10-09", "config_version": "0.3.0"}))
    assert old.fence is None
    # Only a whole number loads: the publish compares it.
    for bad in (True, "3", 1.5, -1):
        with pytest.raises(ValueError):
            LedgerDocument.model_validate_json(json.dumps(
                {"ticker": "KO", "generated_on": "2026-10-09", "config_version": "0.3.0",
                 "fence": bad}))


def test_the_builders_ledger_carries_no_fence_of_its_own(tmp_path):
    """The fence is the publish's to seal; a ledger as the builder writes it
    (the journal's, the auto track's) has no such field, byte for byte as
    before."""
    from app.core.pipeline import analyze
    from app.services.ingestion.companyfacts_mapper import build_dataset
    from app.services.reporting.report_builder import build_report

    facts = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "real"
                        / "companyfacts_KO_trimmed.json").read_text())
    ds, diag = build_dataset(facts, "KO")
    out = tmp_path / "KO.ledger.json"
    build_report(analyze(ds), ds, generated_on="2026-10-09", coverage=diag.coverage(),
                 ticker="KO", fetched_at="2026-10-09 09:00 UTC", ledger_out=out)
    assert '"fence"' not in out.read_text() and '"generation_id": null' in out.read_text()


# --- two processes: the lower request finishes last -----------------------------------------


def _fenced_publisher(args):
    """One workbench run in its own process (another server on the same
    reports folder): A takes the next request number, then B; both stage;
    the one told to go ``first`` publishes, then the other."""
    report, tag, first, events, barrier = args
    if tag == "B":
        events["asked"].wait(30)
    fence = fencing.request("KO")
    if tag == "A":
        events["asked"].set()
    try:
        with replacing(report, fence=fencing.fence("KO", fence)) as staged:
            staged.report.write_text(f"# {tag} report\n")
            staged.ledger.write_text("{}")
            barrier.wait(30)                # both staged before either publishes
            if not first:
                events["published"].wait(30)
    except Superseded as e:
        return tag, fence, "superseded", e.generation_id
    finally:
        if first:
            events["published"].set()
    return tag, fence, "published", staged.generation_id


@pytest.mark.parametrize("lower_last", [True, False], ids=["lower-finishes-last", "in-order"])
def test_two_processes_the_higher_request_is_live_whichever_finishes_last(reports, lower_last):
    import multiprocessing as mp

    report = reports / "workbench" / NAME
    report.parent.mkdir(parents=True)
    ctx = mp.get_context("fork")
    with ctx.Manager() as manager:
        events = {"asked": manager.Event(), "published": manager.Event()}
        barrier = manager.Barrier(2)
        with ctx.Pool(2) as pool:
            results = pool.map(_fenced_publisher, [
                (report, "A", not lower_last, events, barrier),
                (report, "B", lower_last, events, barrier)])
    by_tag = {tag: (fence, state, gid) for tag, fence, state, gid in results}
    assert by_tag["A"][0] == 1 and by_tag["B"][0] == 2
    live = read_live(report)
    assert live is not None and live.generation_id == by_tag["B"][2] and "B report" in live.text
    assert by_tag["B"][1] == "published"
    assert by_tag["A"][1] == ("superseded" if lower_last else "published")
    assert {g.name.rsplit("_", 1)[1] for g in generations(report)} == {
        by_tag["A"][2], by_tag["B"][2]}


# --- the request counter ----------------------------------------------------------------


def test_each_request_takes_the_next_number_across_registries(reports):
    """Two servers on one reports folder share the counter: it is a file,
    taken under a lock, not a number in one process's memory."""
    assert [fencing.request("KO") for _ in range(3)] == [1, 2, 3]
    assert fencing.request("CRM") == 1  # one counter per ticker
    counter = reports / "workbench" / fencing.EPOCHS_DIR / "KO"
    assert counter.read_text() == "3\n"
    assert fencing.current("KO") == 3 and fencing.current("AAPL") == 0


def _requester(args):
    """Take request numbers in another process, its read-to-write window
    widened so two unlocked increments would interleave."""
    import time as _time

    barrier, n = args
    real = fencing.report_files.write_atomic

    def slow(path, text, **kw):
        _time.sleep(0.05)
        real(path, text, **kw)

    fencing.report_files.write_atomic = slow  # this process only
    barrier.wait(30)
    return [fencing.request("KO") for _ in range(n)]


def test_concurrent_requests_from_two_processes_never_share_a_number(reports):
    """The counter is read, incremented and written under its lock: two
    servers asking at once get distinct numbers, never one twice."""
    import multiprocessing as mp

    (reports / "workbench").mkdir(parents=True)
    ctx = mp.get_context("fork")
    with ctx.Manager() as manager:
        barrier = manager.Barrier(2)
        with ctx.Pool(2) as pool:
            got = pool.map(_requester, [(barrier, 5), (barrier, 5)])
    numbers = sorted(n for part in got for n in part)
    assert numbers == list(range(1, 11))
    assert fencing.current("KO") == 10


def test_a_missing_counter_starts_above_every_live_run(reports):
    """A counter lost (the folder copied without its hidden files) must not
    start again at 1 under a live run sealed with 40: every run after it
    would be superseded. It starts above the ticker's live fences."""
    for day, fence in (("2026-10-09", 7), ("2026-10-08", 40)):
        _publish(reports / "workbench" / f"KO_{day}.md", day, fence=fence)
    # The kept runs alone say it: the mark is gone with the counter.
    (reports / "workbench" / fencing.EPOCHS_DIR / "KO.published").unlink()
    assert fencing.request("KO") == 41
    assert fencing.request("KO") == 42


def test_a_missing_counter_whose_runs_cannot_be_listed_fails_closed(reports, tmp_path):
    """Starting the counter again needs every kept run's fence: a ticker
    whose runs cannot all be read is refused, not started at 1."""
    folder = reports / "workbench"
    folder.mkdir(parents=True)
    (folder / NAME).write_text("# a live name\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / report_files.GENERATIONS_DIR).symlink_to(elsewhere)
    with pytest.raises(fencing.EpochError) as e:
        fencing.request("KO")
    assert "cannot all be read" in str(e.value) and "no run started" in str(e.value)
    assert not (folder / fencing.EPOCHS_DIR / "KO").exists()


def test_a_kept_run_from_before_generations_is_not_read_for_a_fence(reports):
    """Plain files at a live name (no generation) carry no sealed fence;
    their ledger is not read as a generation's."""
    folder = reports / "workbench"
    folder.mkdir(parents=True)
    (folder / NAME).write_text("# plain\n")
    (folder / "KO_2026-10-09.ledger.json").write_text('{"fence": 50}')
    assert fencing.request("KO") == 1


@pytest.mark.parametrize("text", ["abc", "", "-1", "1.5", "1 2", "٣", "1\n2\n", "007"])
def test_a_corrupt_counter_fails_closed_and_is_not_reset(reports, text):
    counter = reports / "workbench" / fencing.EPOCHS_DIR / "KO"
    counter.parent.mkdir(parents=True)
    counter.write_text(text)
    with pytest.raises(fencing.EpochError) as e:
        fencing.request("KO")
    assert ".epochs/KO" in str(e.value) and "no run started" in str(e.value)
    assert counter.read_text() == text  # never reset


def test_a_counter_or_folder_that_is_a_symlink_is_refused(reports, tmp_path):
    folder = reports / "workbench" / fencing.EPOCHS_DIR
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("5\n")
    (folder / "KO").symlink_to(target)
    with pytest.raises(fencing.EpochError) as e:
        fencing.request("KO")
    assert "a symlink, never followed" in str(e.value)
    assert target.read_text() == "5\n"
    shutil.rmtree(folder)
    other = tmp_path / "other"
    other.mkdir()
    folder.symlink_to(other)
    with pytest.raises(fencing.EpochError):
        fencing.request("KO")
    assert list(other.iterdir()) == []


def test_a_lock_planted_as_a_symlink_is_not_followed(reports, tmp_path):
    folder = reports / "workbench" / fencing.EPOCHS_DIR
    folder.mkdir(parents=True)
    target = tmp_path / "victim"
    (folder / "KO.lock").symlink_to(target)
    with pytest.raises(fencing.EpochError):
        fencing.request("KO")
    assert not target.exists()


# --- the job registry: Hermes's sequence, in one process ------------------------------------


def _clock(monkeypatch, start: datetime) -> list[datetime]:
    now = [start]
    monkeypatch.setattr(jobs, "_now", lambda: now[0])
    return now


def _held_build(monkeypatch, *, fail_second: bool = False):
    """`reporting.build_report` as the workbench calls it, publishing through
    `replacing` with whatever fence the run passes. The first call stages
    its report and holds until released (a hung SEC read, then a publish);
    with ``fail_second`` the second call raises before publishing."""
    gate, entered, calls = threading.Event(), threading.Event(), []

    def build(ticker, with_docs=True, report_day=None, fresh=False, out_dir=None,
              publish_timeout=None, **kw):
        n = len(calls)
        calls.append(kw["fence"].number if kw.get("fence") is not None else None)
        if fail_second and n == 1:
            raise RuntimeError("SEC answered 503")
        out = out_dir / f"{ticker}_{date.today().isoformat()}.md"
        fenced = {"fence": kw["fence"]} if "fence" in kw else {}
        with replacing(out, timeout=publish_timeout, **fenced) as staged:
            staged.report.write_text(f"# run {'AB'[n]}\n")
            staged.ledger.write_text("{}")
            if n == 0:
                entered.set()
                gate.wait(20)
        return out, "no acute signals"

    monkeypatch.setattr(reporting, "build_report", build)
    return gate, entered, calls


def _live_text(ticker: str) -> str:
    live = read_live(reporting.REPORTS / "workbench" / f"{ticker}_{date.today().isoformat()}.md")
    assert live is not None
    return live.text


def test_hermes_sequence_a_stalled_run_finishing_last_is_superseded(reports, monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    gate, entered, calls = _held_build(monkeypatch)
    reg = jobs.Registry()
    a = reg.start("KO")
    assert entered.wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    b = reg.start("KO")                     # the operator restarts the stalled run
    b_done = reg.wait(b.id, timeout=10)
    assert b_done.state == jobs.DONE, b_done
    assert _live_text("KO").startswith("# run B")
    gate.set()                              # A ends, last
    a_done = reg.wait(a.id, timeout=10)
    assert calls == [1, 2]
    live = read_live(reporting.REPORTS / "workbench" / f"KO_{date.today().isoformat()}.md")
    assert live.text.startswith("# run B") and live.generation_id == b_done.generation_id
    assert a_done.state == jobs.SUPERSEDED, a_done
    assert a_done.generation_id is not None and a_done.generation_id != live.generation_id
    report = reporting.REPORTS / "workbench" / f"KO_{date.today().isoformat()}.md"
    assert any(g.name.endswith(a_done.generation_id) for g in generations(report))
    # The ticker's run is still B, done; A never shows as the winner.
    assert reg.latest("KO").id == b.id and reg.latest("KO").state == jobs.DONE


def test_a_newer_request_that_fails_leaves_the_older_free_to_publish(reports, monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    gate, entered, calls = _held_build(monkeypatch, fail_second=True)
    reg = jobs.Registry()
    a = reg.start("KO")
    assert entered.wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    b = reg.start("KO")
    assert reg.wait(b.id, timeout=10).state == jobs.FAILED
    gate.set()
    a_done = reg.wait(a.id, timeout=10)
    assert calls == [1, 2]
    assert a_done.state == jobs.DONE, a_done
    assert _live_text("KO").startswith("# run A")


def test_a_corrupt_counter_fails_the_run_readably_and_publishes_nothing(reports, monkeypatch):
    built: list = []
    monkeypatch.setattr(reporting, "build_report", lambda *a, **k: built.append(a))
    counter = reports / "workbench" / fencing.EPOCHS_DIR / "KO"
    counter.parent.mkdir(parents=True)
    counter.write_text("twelve\n")
    reg = jobs.Registry()
    t0 = time.monotonic()
    job = reg.start("KO")
    done = reg.wait(job.id, timeout=10)
    assert time.monotonic() - t0 < 5  # ended at once: a waiter is woken
    assert done.state == jobs.FAILED and built == []
    assert ".epochs/KO" in done.error and "no run started" in done.error
    assert "Traceback" not in done.error and done.finished_at is not None
    assert not (reports / "workbench" / report_files.GENERATIONS_DIR).exists()
    assert counter.read_text() == "twelve\n"
    # Not a run in flight: a start after it is tried again (and refused again).
    assert reg.active() == [] and reg.start("KO").id != job.id


def test_each_run_asked_for_is_given_the_next_number(reports, monkeypatch):
    seen: list = []
    monkeypatch.setattr(jobs, "_build", lambda t, fresh, fence: seen.append(fence.number))
    reg = jobs.Registry()
    for _ in range(3):
        reg.wait(reg.start("KO").id, timeout=10)
    assert seen == [1, 2, 3]
    assert [j.fence for j in (reg.latest("KO"),)] == [3]


def test_a_double_click_asks_for_one_number_not_two(reports, monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def build(t, fresh, fence):
        entered.set()
        gate.wait(10)

    monkeypatch.setattr(jobs, "_build", build)
    reg = jobs.Registry()
    first = reg.start("KO")
    assert entered.wait(10)
    assert reg.start("KO").id == first.id
    gate.set()
    reg.wait(first.id, timeout=10)
    assert fencing.current("KO") == 1


def test_the_servers_registry_fences_its_runs():
    assert jobs.REGISTRY._fence is jobs._request_fence


# --- what the page says ---------------------------------------------------------------------


def test_a_superseded_run_is_said_as_such_never_as_done(reports, monkeypatch):
    from fastapi.testclient import TestClient

    from app.web import app

    monkeypatch.setattr(jobs, "REGISTRY", jobs.Registry())
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    gate, entered, _ = _held_build(monkeypatch)
    a = jobs.start("KO")
    assert entered.wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    b = jobs.REGISTRY.wait(jobs.start("KO").id, timeout=10)
    gate.set()
    a_done = jobs.REGISTRY.wait(a.id, timeout=10)
    assert a_done.state == jobs.SUPERSEDED
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    # The status box is the ticker's newest run, B, the live one.
    frag = client.get("/t/KO/status").text
    assert 'data-state="done"' in frag and b.generation_id in frag
    assert "is the live run" in frag
    # Were A the newest the registry held, it is said as superseded.
    monkeypatch.setattr(jobs, "latest", lambda t: a_done)
    frag = client.get("/t/KO/status").text
    assert 'data-state="superseded"' in frag and "superseded" in frag
    assert "a newer run is live" in frag and b.generation_id in frag
    assert a_done.generation_id in frag and "History" in frag
    assert "published generation" not in frag


def test_a_done_run_no_longer_live_says_which_run_is(reports, monkeypatch):
    """Another process published after this run: its "done" is not the
    live card, and the box says which run is."""
    from fastapi.testclient import TestClient

    from app.web import app

    monkeypatch.setattr(jobs, "REGISTRY", jobs.Registry())
    gid = {}

    def build(ticker, with_docs=True, report_day=None, fresh=False, out_dir=None, **kw):
        out = out_dir / f"{ticker}_{date.today().isoformat()}.md"
        gid["mine"] = _publish(out, "mine", fence=kw.get("fence"))
        return out, "ok"

    monkeypatch.setattr(reporting, "build_report", build)
    job = jobs.REGISTRY.wait(jobs.start("KO").id, timeout=10)
    assert job.state == jobs.DONE
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert "is the live run" in client.get("/t/KO/status").text
    other = _publish(reports / "workbench" / f"KO_{date.today().isoformat()}.md", "other",
                     fence=fencing.fence("KO", fencing.request("KO")))
    frag = client.get("/t/KO/status").text
    assert job.generation_id in frag and other in frag
    assert "is the live run" not in frag and "no longer the live run" in frag


def test_the_poller_reloads_on_a_superseded_run_too(reports):
    from fastapi.testclient import TestClient

    from app.web import app

    js = TestClient(app, base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)).get("/static/app.js").text
    assert 'now === "superseded"' in js



# --- fix round 3 (independent review of 2cbba1c) ---------------------------------------
# The fence was compared with the live generation of ONE day's report only:
# a run asked for before midnight that published after it took a new day's
# file, where nothing was live, and became the card (H1); a restore or an
# unfenced publish dropped the live fence, so an abandoned older run replaced
# the operator's restore (M2); an unreadable live ledger read as "no fence"
# (M1); a counter behind the kept runs was trusted (L1). The ticker's
# high-water mark of PUBLISHED fences (`.epochs/<T>.published`), read and
# raised under the ticker's lock around the switch, holds across days and
# restores.


def test_a_run_asked_for_earlier_never_publishes_on_another_day_over_a_later_one(tmp_path):
    day1, day2 = tmp_path / "KO_2026-10-09.md", tmp_path / "KO_2026-10-10.md"
    b = _publish(day1, "B", fence=2)
    with pytest.raises(Superseded) as e:
        _publish(day2, "A", fence=1)
    assert e.value.live_fence == 2
    assert read_live(day1).generation_id == b and read_live(day2) is None
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "2\n"
    # A later request publishes on either day, and raises the mark.
    _publish(day2, "C", fence=3)
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "3\n"


def test_a_restore_is_not_replaced_by_a_run_asked_for_before_the_newest(tmp_path):
    report = tmp_path / NAME
    x = _publish(report, "run1", fence=1)
    _publish(report, "run3", fence=3)
    report_files.restore(report, x)
    with pytest.raises(Superseded):
        _publish(report, "run2-abandoned", fence=2)
    assert read_live(report).generation_id == x
    # A run asked for after the restore publishes as usual.
    new = _publish(report, "run4", fence=4)
    assert read_live(report).generation_id == new


def test_an_unfenced_publish_does_not_lower_the_mark(tmp_path):
    report = tmp_path / NAME
    _publish(report, "run3", fence=3)
    cli = _publish(report, "cli")
    with pytest.raises(Superseded):
        _publish(report, "run2-abandoned", fence=2)
    assert read_live(report).generation_id == cli


def test_a_newer_request_that_failed_never_raised_the_mark(tmp_path):
    report = tmp_path / NAME
    _publish(report, "run1", fence=1)
    with pytest.raises(RuntimeError), replacing(report, fence=_fence(report, 3)):
        raise RuntimeError("SEC answered 503")
    gid = _publish(report, "run2", fence=2)
    assert read_live(report).generation_id == gid
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "2\n"


@pytest.mark.parametrize("err", [errno.EMFILE, errno.EIO, errno.EACCES],
                         ids=["EMFILE", "EIO", "EACCES"])
def test_a_live_fence_that_cannot_be_read_fails_the_publish_closed(tmp_path, monkeypatch, err):
    """M1: a failed READ of the live ledger is not "no fence": the fenced
    publish is refused, set apart, and the live run untouched."""
    report = tmp_path / NAME
    newer = _publish(report, "B", fence=2)
    (tmp_path / fencing.EPOCHS_DIR / "KO.published").unlink()  # only the ledger can say
    real = Path.read_text

    def flaky(self, *a, **k):
        if self.name.endswith(".ledger.json") and report_files.STAGING_DIR not in self.parts:
            raise OSError(err, os.strerror(err))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(report_files.NotPublished) as e:
        _publish(report, "A", fence=1)
    monkeypatch.setattr(Path, "read_text", real)
    assert "live fence unreadable" in str(e.value) and errno.errorcode[err] in str(e.value)
    assert read_live(report).generation_id == newer
    assert len(generations(report)) == 1
    home = tmp_path / report_files.GENERATIONS_DIR / "KO_2026-10-09"
    assert len([d for d in home.iterdir() if d.name.startswith(".failed-")]) == 1


def test_a_high_water_mark_that_cannot_be_read_fails_the_publish_closed(tmp_path):
    report = tmp_path / NAME
    newer = _publish(report, "B", fence=2)
    mark = tmp_path / fencing.EPOCHS_DIR / "KO.published"
    mark.unlink()
    mark.mkdir()  # EISDIR on read
    with pytest.raises(report_files.NotPublished) as e:
        _publish(report, "A", fence=3)
    assert "high-water mark unreadable" in str(e.value)
    assert read_live(report).generation_id == newer
    mark.rmdir()
    mark.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(report_files.NotPublished):
        _publish(report, "A", fence=3)
    assert not (tmp_path / "elsewhere").exists()
    mark.unlink()
    mark.write_text("not a number\n")
    with pytest.raises(report_files.NotPublished) as e:
        _publish(report, "A", fence=3)
    assert "high-water mark unreadable" in str(e.value)
    assert read_live(report).generation_id == newer


def test_a_counter_behind_the_kept_runs_or_the_mark_starts_above_them(reports):
    """L1: a counter restored from an older backup (or edited) is not
    trusted below what was kept or published."""
    report = reports / "workbench" / NAME
    report.parent.mkdir(parents=True)
    for _ in range(5):
        _publish(report, "run", fence=fencing.fence("KO", fencing.request("KO")))
    counter = reports / "workbench" / fencing.EPOCHS_DIR / "KO"
    counter.write_text("2\n")
    assert fencing.request("KO") == 6
    # The mark alone (its runs gone from the folder) still counts.
    shutil.rmtree(reports / "workbench" / report_files.GENERATIONS_DIR)
    counter.write_text("1\n")
    (reports / "workbench" / fencing.EPOCHS_DIR / "KO.published").write_text("9\n")
    assert fencing.request("KO") == 10


def test_a_superseded_run_is_not_news_that_hides_a_failure(reports):
    """L2: `published_since` (a failed run's banner is dropped once a run
    has been published after it) counted a superseded generation, which was
    never live, and hid the ticker's latest failure."""
    from app.services.workbench import views

    report = reports / "workbench" / f"KO_{date.today().isoformat()}.md"
    report.parent.mkdir(parents=True)
    t0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
    _publish(report, "C", fence=fencing.fence("KO", 2), now=t0)
    failed_at = t0 + timedelta(seconds=5)
    with pytest.raises(Superseded):
        _publish(report, "A", fence=fencing.fence("KO", 1), now=t0 + timedelta(seconds=9))
    assert not views.published_since("KO", failed_at)
    refs = views.runs("KO")
    assert [r.superseded for r in refs] == [True, False]
    # A superseded run restored by hand is live: it counts.
    a = next(r for r in refs if r.superseded)
    report_files.restore(report, a.name)
    assert views.published_since("KO", failed_at)
    report_files.restore(report, next(r for r in refs if not r.superseded).name)
    assert not views.published_since("KO", failed_at)
    # A run that was live and then replaced still counts as published.
    _publish(report, "D", fence=fencing.fence("KO", 3), now=t0 + timedelta(seconds=20))
    assert views.published_since("KO", failed_at)


def test_the_job_carries_its_fence_from_the_start(reports, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(jobs, "_build", lambda t, fresh, fence: gate.wait(10))
    reg = jobs.Registry()
    job = reg.start("KO")
    assert job.fence == 1 and reg.get(job.id).fence == 1
    gate.set()
    reg.wait(job.id, timeout=10)


def _hold(lock: Path, held: threading.Event, release: threading.Event) -> None:
    import fcntl

    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.set()
        release.wait(30)
    finally:
        os.close(fd)


def test_a_stuck_holder_of_the_tickers_lock_fails_a_request_readably(reports, monkeypatch):
    """N4: the request counter's lock is taken under the registry's lock:
    a holder that never let go froze every page. It is waited for at most
    `review.PUBLISH_WAIT_S`, then the run fails, said."""
    from app.services.journal import review

    monkeypatch.setattr(review, "PUBLISH_WAIT_S", 0.3)
    built: list = []
    monkeypatch.setattr(jobs, "_build", lambda *a: built.append(a))
    lock = reports / "workbench" / fencing.EPOCHS_DIR / "KO.lock"
    held, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold, args=(lock, held, release), daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        out: list = []
        starter = threading.Thread(target=lambda: out.append(jobs.Registry().start("KO")),
                                   daemon=True)
        starter.start()
        starter.join(10)
        assert not starter.is_alive(), "start() waited on the lock for good"
    finally:
        release.set()
        holder.join(10)
    (job,) = out
    assert job.state == jobs.FAILED and "held" in job.error and "no run started" in job.error
    assert "another workbench process stuck?" in job.error
    assert built == []


def test_a_stuck_holder_of_the_tickers_lock_fails_a_publish_as_busy(tmp_path):
    report = tmp_path / NAME
    held, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold, args=(tmp_path / fencing.EPOCHS_DIR / "KO.lock",
                                                  held, release), daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        t0 = time.monotonic()
        with pytest.raises(report_files.PublishBusy):
            _publish(report, "A", fence=1, timeout=0.3)
        assert time.monotonic() - t0 < 5
    finally:
        release.set()
        holder.join(10)
    assert read_live(report) is None



def test_a_lost_mark_still_leaves_the_live_runs_own_fence(tmp_path):
    """The live generation's fence still counts when the mark is missing
    (runs fenced before the mark existed; the folder copied without it)."""
    report = tmp_path / NAME
    newer = _publish(report, "B", fence=2)
    (tmp_path / fencing.EPOCHS_DIR / "KO.published").unlink()
    with pytest.raises(Superseded):
        _publish(report, "A", fence=1)
    assert read_live(report).generation_id == newer


def test_a_mark_that_cannot_be_raised_publishes_nothing(tmp_path, monkeypatch):
    """A run live with the mark below it could be replaced by an older
    request on another day: the mark is raised first, and a failed raise
    publishes nothing."""
    report = tmp_path / NAME
    older = _publish(report, "A", fence=1)

    def refuse(path, n):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(report_files, "write_count", refuse)
    with pytest.raises(report_files.NotPublished) as e:
        _publish(report, "B", fence=2)
    assert "raising the high-water mark to 2 failed" in str(e.value)
    assert read_live(report).generation_id == older
    assert len(generations(report)) == 1
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "1\n"


# --- fix round 4 (Hermes re-audit of #118 @ 34836cf) -----------------------------------
# The mark was raised AFTER the switch, as a second durable step: a process
# killed between the two left the run live with the mark below it, and an
# older run for another report day, seeing only the mark, published. Now the
# mark is raised before the switch (written and its folder fsynced), and the
# comparison also takes every report day's live fence, so a lost or rewound
# mark cannot admit an older run either.

DAY_A, DAY_B = "KO_2026-10-09.md", "KO_2026-10-10.md"


def _crash_publisher(args):
    """Publish fence 3 on day A in this (forked) process and SIGKILL it at
    ``kill_at``: right after the mark is written, or right after the
    pointer's switch. Never returns."""
    import signal

    report, kill_at = args
    real_write, real_switch = report_files.write_count, report_files._switch

    def write_count(path, n):
        real_write(path, n)
        if kill_at == "after_mark" and path.name.endswith(".published"):
            os.kill(os.getpid(), signal.SIGKILL)

    def switch(*a, **k):
        real_switch(*a, **k)
        if kill_at == "after_switch":
            os.kill(os.getpid(), signal.SIGKILL)

    report_files.write_count = write_count
    report_files._switch = switch
    _publish(report, "C", fence=3)
    os._exit(3)  # not reached: the kill came first


def _killed_mid_publish(tmp_path, kill_at):
    import multiprocessing as mp

    day_a = tmp_path / DAY_A
    first = _publish(day_a, "A", fence=1)
    proc = mp.get_context("fork").Process(target=_crash_publisher, args=((day_a, kill_at),))
    proc.start()
    proc.join(60)
    assert proc.exitcode == -9, proc.exitcode
    return day_a, first


@pytest.mark.parametrize("kill_at", ["after_mark", "after_switch"])
def test_a_publisher_killed_mid_publish_never_lets_an_older_run_in_on_another_day(
        tmp_path, kill_at):
    day_a, first = _killed_mid_publish(tmp_path, kill_at)
    live = read_live(day_a)  # the pointer resolves: never dangling
    assert live is not None
    if kill_at == "after_mark":
        assert live.generation_id == first  # killed before the switch: the earlier run
    else:
        assert live.text.startswith("# C report") and _ledger(day_a)["fence"] == 3
    with pytest.raises(Superseded):
        _publish(tmp_path / DAY_B, "B", fence=2)
    assert read_live(tmp_path / DAY_B) is None


@pytest.mark.parametrize("mark", ["deleted", "rewound"])
def test_after_a_kill_a_lost_or_rewound_mark_still_refuses_an_older_run(tmp_path, mark):
    day_a, _ = _killed_mid_publish(tmp_path, "after_switch")
    path = tmp_path / fencing.EPOCHS_DIR / "KO.published"
    if mark == "deleted":
        path.unlink()
    else:
        path.write_text("1\n")
    with pytest.raises(Superseded) as e:
        _publish(tmp_path / DAY_B, "B", fence=2)
    assert e.value.live_fence == 3
    assert _ledger(day_a)["fence"] == 3 and read_live(tmp_path / DAY_B) is None


@pytest.mark.parametrize("mark", ["deleted", "rewound"])
def test_a_lost_mark_is_covered_by_every_days_live_run(tmp_path, mark):
    _publish(tmp_path / DAY_A, "C", fence=3)
    path = tmp_path / fencing.EPOCHS_DIR / "KO.published"
    if mark == "deleted":
        path.unlink()
    else:
        path.write_text("1\n")
    with pytest.raises(Superseded):
        _publish(tmp_path / DAY_B, "B", fence=2)
    # A replay of a day is not one of the ticker's live days: with the mark
    # lost again, its fence holds nothing back.
    _publish(tmp_path / "KO_2026-10-08.replay.md", "replay", fence=9)
    path.unlink()
    gid = _publish(tmp_path / DAY_B, "D", fence=4)
    assert read_live(tmp_path / DAY_B).generation_id == gid


def test_another_tickers_runs_do_not_hold_this_one_back(tmp_path):
    epochs = tmp_path / fencing.EPOCHS_DIR
    _publish(tmp_path / "KOF_2026-10-09.md", "other", fence=report_files.Fence(
        9, epochs / "KOF.published", epochs / "KOF.lock"))
    gid = _publish(tmp_path / DAY_B, "B", fence=2)
    assert read_live(tmp_path / DAY_B).generation_id == gid


def test_an_interrupt_during_the_mark_write_restores_the_mark_and_publishes_nothing(
        tmp_path, monkeypatch):
    day_a = tmp_path / DAY_A
    first = _publish(day_a, "A", fence=1)
    real = report_files.write_count

    def interrupted(path, n):
        real(path, n)
        raise KeyboardInterrupt

    monkeypatch.setattr(report_files, "write_count", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _publish(day_a, "C", fence=3)
    monkeypatch.setattr(report_files, "write_count", real)
    assert current_generation(day_a) is not None
    assert read_live(day_a).generation_id == first
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "1\n"
    assert len(generations(day_a)) == 1


def test_an_interrupt_right_after_the_switch_switches_back_never_leaving_a_dangling_pointer(
        tmp_path, monkeypatch):
    day_a = tmp_path / DAY_A
    first = _publish(day_a, "A", fence=1)
    real = report_files._switch

    def switch(*a, **k):
        real(*a, **k)
        raise KeyboardInterrupt

    monkeypatch.setattr(report_files, "_switch", switch)
    with pytest.raises(KeyboardInterrupt):
        _publish(day_a, "C", fence=3)
    monkeypatch.setattr(report_files, "_switch", real)
    gen = current_generation(day_a)  # resolves: not a set-apart directory
    assert gen is not None and not gen.name.startswith(".failed-")
    assert read_live(day_a).generation_id == first
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "1\n"
    # A switch back that fails too leaves the new run where the pointer
    # names it, and says so (PublishInDoubt), never sets it apart.
    monkeypatch.setattr(report_files, "_switch", switch)

    def no_way_back(*a, **k):
        raise report_files.PublishInDoubt("injected: switching back failed")

    monkeypatch.setattr(report_files, "_switch_back", no_way_back)
    with pytest.raises(report_files.PublishInDoubt):
        _publish(day_a, "D", fence=4)
    gen = current_generation(day_a)
    assert gen is not None and gen.is_dir() and read_live(day_a).text.startswith("# D report")


def _fsynced(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = os.fsync

    def fsync(fd):
        seen.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return seen


def test_the_counter_and_the_mark_are_durable_with_their_folder(reports, monkeypatch):
    """`os.replace` is durable only once its directory is fsynced: a power
    loss could otherwise lose the counter's or the mark's new value."""
    seen = _fsynced(monkeypatch)
    folder = str(reports / "workbench" / fencing.EPOCHS_DIR)
    assert fencing.request("KO") == 1
    assert seen and seen[-1] == folder
    seen.clear()
    report = reports / "workbench" / DAY_A
    _publish(report, "A", fence=fencing.fence("KO", 1))
    assert folder in seen  # the mark's folder, after its rename


def test_write_atomic_is_durable_only_when_asked(tmp_path, monkeypatch):
    seen = _fsynced(monkeypatch)
    report_files.write_atomic(tmp_path / "plain", "x")
    assert str(tmp_path) not in seen
    report_files.write_atomic(tmp_path / "durable", "x", durable=True)
    assert seen[-1] == str(tmp_path)


def _killed_at_step(args):
    """Publish fence 3 on day A; SIGKILL on entering the ``k``-th fsync of
    the publish (each durable step: the sealed files, the staged folder,
    the generations folder, the mark, the live names, the pointer)."""
    import signal

    report, k = args
    real, n = report_files._fsync, [0]

    def fsync(path):
        n[0] += 1
        if n[0] == k:
            os.kill(os.getpid(), signal.SIGKILL)
        real(path)

    report_files._fsync = fsync
    _publish(report, "C", fence=3)
    os._exit(0)


@pytest.mark.parametrize("k", range(1, 9))
def test_a_kill_at_any_step_leaves_one_run_live_and_no_older_run_after_it(tmp_path, k):
    """The publish transaction under SIGKILL at every durable step: the
    pointer always resolves, the live run is the earlier one or the new one,
    never anything else, and once the new one is live no older request
    publishes on any day; a request asked for after the crash publishes."""
    import multiprocessing as mp

    day_a = tmp_path / DAY_A
    first = _publish(day_a, "A", fence=1)
    proc = mp.get_context("fork").Process(target=_killed_at_step, args=((day_a, k),))
    proc.start()
    proc.join(60)
    assert proc.exitcode in (-9, 0), proc.exitcode
    live = read_live(day_a)
    assert live is not None
    fence = _ledger(day_a)["fence"]
    assert (live.generation_id == first) == (fence == 1) and fence in (1, 3)
    if fence == 3:
        with pytest.raises(Superseded):
            _publish(tmp_path / DAY_B, "B", fence=2)
    mark = read_count(tmp_path / fencing.EPOCHS_DIR / "KO.published")
    assert mark is not None and mark >= fence  # never behind the live run
    gid = _publish(tmp_path / DAY_B, "D", fence=4)
    assert read_live(tmp_path / DAY_B).generation_id == gid


# --- fix round 5 (independent review of 6bf9f9e) ---------------------------------------


def test_a_failed_publish_puts_the_mark_back_no_lower_than_a_live_fence(tmp_path, monkeypatch):
    """L1: the rollback put back the mark it READ, even when the scan had
    just seen a higher live fence on another day (a mark lost or rewound):
    with that day then set aside, an older request published."""
    _publish(tmp_path / DAY_B, "B", fence=2)
    mark = tmp_path / fencing.EPOCHS_DIR / "KO.published"
    mark.write_text("1\n")  # rewound
    real = report_files._symlink

    def failing(link, target):
        if link.name == "current" and "KO_2026-10-09" in str(link):
            raise OSError(errno.EIO, "injected: the pointer's rename failed")
        return real(link, target)

    monkeypatch.setattr(report_files, "_symlink", failing)
    with pytest.raises(report_files.NotPublished):
        _publish(tmp_path / DAY_A, "A", fence=3)
    monkeypatch.setattr(report_files, "_symlink", real)
    assert mark.read_text() == "2\n"
    report_files.set_aside(tmp_path / DAY_B)
    with pytest.raises(Superseded):
        _publish(tmp_path / "KO_2026-10-11.md", "C", fence=1)


def test_a_failed_first_fenced_publish_removes_the_mark_it_wrote(tmp_path, monkeypatch):
    """The put-back's other branch: no mark before, none after."""
    _publish(tmp_path / DAY_A, "A")  # unfenced: no mark
    real = report_files._symlink

    def failing(link, target):
        if link.name == "current":
            failing.n += 1
            if failing.n == 1:
                raise OSError(errno.EIO, "injected")
        return real(link, target)

    failing.n = 0
    monkeypatch.setattr(report_files, "_symlink", failing)
    with pytest.raises(report_files.NotPublished):
        _publish(tmp_path / DAY_A, "C", fence=3)
    assert not (tmp_path / fencing.EPOCHS_DIR / "KO.published").exists()


def test_the_message_names_the_step_that_failed(tmp_path, monkeypatch):
    """L3: a failure linking the live names was said as "raising the
    high-water mark to 3 failed"."""
    _publish(tmp_path / DAY_A, "A", fence=1)

    def failing(report):
        raise OSError(errno.ENOSPC, "injected: no space")

    monkeypatch.setattr(report_files, "_link_live_names", failing)
    with pytest.raises(report_files.NotPublished) as e:
        _publish(tmp_path / DAY_A, "C", fence=3)
    assert "linking the live names failed" in str(e.value)
    assert "raising the high-water mark" not in str(e.value)
    monkeypatch.undo()

    def refuse(path, n):
        raise OSError(errno.ENOSPC, "injected: no space")

    monkeypatch.setattr(report_files, "write_count", refuse)
    with pytest.raises(report_files.NotPublished) as e:
        _publish(tmp_path / DAY_A, "D", fence=4)
    assert "raising the high-water mark to 4 failed" in str(e.value)


def test_a_verified_rollback_is_not_switched_back_again(tmp_path, monkeypatch):
    """L4: `_switch` rolls back and reads the pointer back on its own
    failure; a second switch back that met a transient error turned that
    verified NotPublished into PublishInDoubt."""
    day_a = tmp_path / DAY_A
    first = _publish(day_a, "A", fence=1)
    home = tmp_path / report_files.GENERATIONS_DIR / "KO_2026-10-09"
    real, seen = report_files._fsync, [0]

    def flaky(path):
        # The generations folder's fsyncs during the publish: #1 before the
        # switch works, #2 (in `_switch`, after the pointer moved) fails, #3
        # (`_switch`'s own switch back) works, and a #4 (a second switch
        # back) would fail.
        if Path(path) == home:
            seen[0] += 1
            if seen[0] in (2, 4):
                raise OSError(errno.EIO, f"injected EIO #{seen[0]}")
        return real(path)

    monkeypatch.setattr(report_files, "_fsync", flaky)
    with pytest.raises(report_files.NotPublished):
        _publish(day_a, "C", fence=3)
    monkeypatch.setattr(report_files, "_fsync", real)
    assert read_live(day_a).generation_id == first
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "1\n"
    assert any(g.name.startswith(".failed-") for g in home.iterdir())


def test_a_switch_in_doubt_is_never_set_apart(tmp_path, monkeypatch):
    """`_switch` itself ending in doubt: the new run may be live, so it
    stays where the pointer may name it and the mark stays raised."""
    day_a = tmp_path / DAY_A
    _publish(day_a, "A", fence=1)
    real = report_files._switch

    def in_doubt(*a, **k):
        real(*a, **k)
        raise report_files.PublishInDoubt("injected: in doubt")

    monkeypatch.setattr(report_files, "_switch", in_doubt)
    with pytest.raises(report_files.PublishInDoubt):
        _publish(day_a, "C", fence=3)
    gen = current_generation(day_a)
    assert gen is not None and read_live(day_a).text.startswith("# C report")
    assert (tmp_path / fencing.EPOCHS_DIR / "KO.published").read_text() == "3\n"


def _kill_before_switch(args):
    import signal

    report = args
    real = report_files.write_count

    def write_count(path, n):
        real(path, n)
        if path.name.endswith(".published"):
            os.kill(os.getpid(), signal.SIGKILL)

    report_files.write_count = write_count
    _publish(report, "killed", fence=fencing.fence("KO", fencing.request("KO")))
    os._exit(0)


def test_a_run_killed_before_its_switch_is_listed_as_never_published(reports):
    """L5: the leftover of a publish killed before its switch was listed as
    an ordinary kept run, counted as published news, and restorable. It
    carries a pending marker now: never published, said so, refused."""
    import multiprocessing as mp

    from app.services.workbench import views

    report = reports / "workbench" / DAY_A
    report.parent.mkdir(parents=True)
    first = _publish(report, "first", fence=fencing.fence("KO", fencing.request("KO")))
    before = datetime.now(UTC)
    time.sleep(1.1)  # the leftover is stamped in a later second than `before`
    proc = mp.get_context("fork").Process(target=_kill_before_switch, args=(report,))
    proc.start()
    proc.join(60)
    assert proc.exitcode == -9
    refs = views.runs("KO")
    leftover = [r for r in refs if not r.live]
    assert len(leftover) == 1 and leftover[0].pending
    assert not [r for r in refs if r.live][0].pending
    assert not views.published_since("KO", before)
    assert views.live_run("KO").generation_id == first
    with pytest.raises(ValueError, match="never published"):
        report_files.restore(report, leftover[0].name)
    from fastapi.testclient import TestClient

    from app.web import app

    page = TestClient(app, base_url="http://127.0.0.1",
                      client=("127.0.0.1", 50000)).get("/t/KO").text
    assert "never published: the publishing process stopped" in page.split('id="history"')[1]
    # A published run carries no marker; one live with a marker left (killed
    # after its switch) is cleared by the next publish of its report.
    assert not (current_generation(report) / report_files.PENDING_MARK).exists()


def test_the_card_is_the_highest_fence_live_not_the_newest_day(reports, monkeypatch):
    """M1's belt: request 3 published on day 2, then request 4 on day 1 (the
    days a build named after its fetch). The card follows the fence."""
    from app.services.workbench import views

    wb = reports / "workbench"
    wb.mkdir(parents=True)
    _publish(wb / DAY_B, "three", fence=fencing.fence("KO", 3))
    four = _publish(wb / DAY_A, "four", fence=fencing.fence("KO", 4))
    assert views.live_run("KO").generation_id == four
    assert views.live_generation("KO") == four
    assert views.ticker_view("KO").latest.generation_id == four
    from app.services.watch import watchlist as wl

    monkeypatch.setattr(wl, "WATCHLIST", reports.parent / "watchlist.json")
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    rows, _ = views.watchlist_rows()
    assert [r.latest.generation_id for r in rows] == [four]
    # A run from before fences never outranks one that states its fence.
    _publish(wb / "KO_2026-10-12.md", "unfenced, newer day")
    assert views.live_run("KO").generation_id == four


def test_a_live_run_left_pending_is_live_and_cleared_by_the_next_publish(reports):
    """Killed after its switch, before its marker went: the run is live
    (never listed as "never published"), and the next publish of its
    report clears the marker, so once replaced it reads as published."""
    from app.services.workbench import views

    report = reports / "workbench" / DAY_A
    report.parent.mkdir(parents=True)
    a = _publish(report, "A", fence=fencing.fence("KO", 1))
    gen = current_generation(report)
    (gen / report_files.PENDING_MARK).write_text("pending\n")
    ref = views.live_run("KO")
    assert ref.generation_id == a and ref.live and ref.pending
    # Live, it is a run like any other: restoring it is allowed (a no-op).
    report_files.restore(report, ref.name)
    assert current_generation(report) == gen
    since = datetime.now(UTC) - timedelta(hours=1)
    assert views.published_since("KO", since)
    _publish(report, "B", fence=fencing.fence("KO", 2))
    assert not (gen / report_files.PENDING_MARK).exists()
    assert not next(r for r in views.runs("KO") if r.generation_id == a).pending


def test_a_request_fixes_its_report_day_in_request_order(reports, monkeypatch):
    """M1: the day a run is published for is fixed when it is asked for,
    under the ticker's lock, so day order follows request order."""
    days = iter([date(2026, 10, 9), date(2026, 10, 10)])

    class Day(date):
        @classmethod
        def today(cls):
            return next(days)

    monkeypatch.setattr(fencing, "date", Day)
    first, second = fencing.request_run("KO"), fencing.request_run("KO")
    assert (first.number, first.day) == (1, "2026-10-09")
    assert (second.number, second.day) == (2, "2026-10-10")


def _killed_entering_publish(args):
    import signal

    report = args

    def stop(*a, **k):
        os.kill(os.getpid(), signal.SIGKILL)

    report_files._publish = stop  # the first thing the publish would do
    _publish(report, "C", fence=3)
    os._exit(0)


def test_a_rewound_mark_is_healed_before_the_publish_writes_anything(tmp_path):
    """The fault sweep's lost-mark case (review of 6bf9f9e): day B live at
    fence 2, the mark rewound to 1. A publish stopped at its very first
    step left the mark below the live run; it is healed under the ticker's
    lock before any of the publish is written."""
    import multiprocessing as mp

    _publish(tmp_path / DAY_A, "A", fence=1)
    _publish(tmp_path / DAY_B, "B", fence=2)
    mark = tmp_path / fencing.EPOCHS_DIR / "KO.published"
    mark.write_text("1\n")
    proc = mp.get_context("fork").Process(target=_killed_entering_publish,
                                          args=(tmp_path / DAY_A,))
    proc.start()
    proc.join(60)
    assert proc.exitcode == -9
    assert mark.read_text() == "2\n"
