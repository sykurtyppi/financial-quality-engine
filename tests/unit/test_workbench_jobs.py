"""The workbench's job registry: one report build per ticker, in a thread.

The workbench (r36) is the way in for an operator who types a ticker; its
"Run" button starts `reporting.build_report` — the full publish path — off
the request thread. These pin what the page relies on: a run ends `done`
with the generation it published, or `failed` with one short sentence the
operator can act on (never a traceback); a second click while a run is in
flight is that run, not another build; and the registry stays bounded in a
server that runs for weeks.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.ingestion.sec_client import SecClientError
from app.services.journal import reporting, review
from app.services.reporting.report_files import (
    NotPublished,
    PublishBusy,
    PublishInDoubt,
    read_live,
    replacing,
)
from app.services.workbench import jobs


def _instant(ticker: str, fresh: bool, fence: int | None = None) -> str | None:
    return None


def _registry(build=_instant, **kw) -> jobs.Registry:
    """A registry over a stand-in build, unfenced unless a test says so: a
    fence is a counter file under the reports folder, and these stand-ins
    publish nothing (test_workbench_fencing.py fences the real path)."""
    return jobs.Registry(build=build, **{"fence": None, **kw})


def _finish(reg: jobs.Registry, job: jobs.Job) -> jobs.Job:
    done = reg.wait(job.id, timeout=10)
    assert done is not None and not done.active, done
    return done


def test_a_run_ends_done_and_records_the_generation_it_published(tmp_path, monkeypatch):
    """The default builder is the real publish path: what it records is the
    generation `replacing` made in THIS run (`report_files.recording`), not
    whatever is live when the page next reads."""
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    seen = {}

    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        seen.update(ticker=ticker, with_docs=with_docs, fresh=fresh, report_day=report_day)
        out = reporting.report_path(ticker, report_day)
        out.parent.mkdir(parents=True, exist_ok=True)
        with replacing(out) as staged:
            staged.report.write_text(f"# Decision Card — {ticker}\n")
            staged.ledger.write_text("{}")
        return out, "no acute signals"

    monkeypatch.setattr(reporting, "build_report", build)
    reg = jobs.Registry()
    job = reg.start("ko")
    assert job.ticker == "KO" and job.state in (jobs.QUEUED, jobs.RUNNING)
    done = _finish(reg, job)
    assert done.state == jobs.DONE and done.error is None
    assert done.started_at is not None and done.finished_at is not None
    assert done.finished_at >= done.started_at
    live = read_live(reporting.report_path("KO"))
    assert live is not None and done.generation_id == live.generation_id
    # Cached by default (repeat views are fast); with documents, as the CLI.
    assert seen == {"ticker": "KO", "with_docs": True, "fresh": False, "report_day": None}


def test_fresh_is_passed_through(monkeypatch):
    seen = []
    reg = _registry(build=lambda t, fresh, fence=None: seen.append(fresh))
    _finish(reg, reg.start("KO", fresh=True))
    _finish(reg, reg.start("KO"))
    assert seen == [True, False]


def test_a_second_start_while_running_is_the_same_job():
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def build(ticker, fresh, fence=None):
        calls.append(ticker)
        entered.set()
        gate.wait(10)

    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered.wait(10)
    again = reg.start("KO")  # a double click
    assert again.id == first.id and again.state == jobs.RUNNING
    assert reg.latest("KO").id == first.id
    other = reg.start("CRM")  # another ticker is its own run
    assert other.id != first.id
    gate.set()
    _finish(reg, first)
    _finish(reg, other)
    assert sorted(calls) == ["CRM", "KO"]
    # Once it has finished, a start is a new run.
    third = reg.start("KO")
    assert third.id != first.id
    _finish(reg, third)


def test_an_invalid_ticker_is_refused_before_anything_starts():
    calls = []
    reg = _registry(build=lambda t, f, fence=None: calls.append(t))
    for bad in ("../x", "ko;rm", "", "A" * 13):
        with pytest.raises(ValueError):
            reg.start(bad)
    assert calls == [] and len(reg) == 0


@pytest.mark.parametrize("exc,expected", [
    (SecClientError("HTTP 503 from data.sec.gov"), "SEC fetch failed: HTTP 503 from data.sec.gov"),
    (PublishBusy("KO_2026-10-09.md: a publish has held its lock for over 5s"),
     "another run is publishing this report; try again"),
    (NotPublished("KO_2026-10-09.md: the rebuild wrote no evidence ledger; nothing published"),
     "KO_2026-10-09.md: the rebuild wrote no evidence ledger; nothing published"),
    (ValueError("cannot map the payload"), "ValueError: cannot map the payload"),
])
def test_failures_are_said_in_one_line(monkeypatch, exc, expected):
    monkeypatch.setenv("EDGAR_IDENTITY", "Jane Doe jane@example.com")

    def build(ticker, fresh, fence=None):
        raise exc

    reg = _registry(build=build)
    done = _finish(reg, reg.start("KO"))
    assert done.state == jobs.FAILED and done.error == expected
    assert "Traceback" not in done.error and done.generation_id is None


def test_a_missing_identity_is_a_setup_message_not_a_fetch_failure(monkeypatch):
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)

    def build(ticker, fresh, fence=None):
        raise SecClientError("SEC fair-access rules require identifying yourself. Set "
                             "EDGAR_IDENTITY to e.g. ...")

    reg = _registry(build=build)
    done = _finish(reg, reg.start("KO"))
    assert done.state == jobs.FAILED
    assert done.error.startswith("Setup: EDGAR_IDENTITY is not set")
    assert "SEC fetch failed" not in done.error


def test_a_publish_in_doubt_is_said_as_such_with_its_whole_message():
    message = ("KO_2026-10-09.md: publishing x failed (OSError: EIO), and switching back failed: "
               "the NEW generation x may be live. Check which run is live with `readlink p`; "
               "report_files.restore('p', 'g') makes g live again.")

    def build(ticker, fresh, fence=None):
        raise PublishInDoubt(message)

    reg = _registry(build=build)
    done = _finish(reg, reg.start("KO"))
    # Never shortened: the restore command at its end is the way back.
    assert done.error == f"Report publish IN DOUBT: {message}"


def test_an_error_at_the_limit_is_kept_whole_and_one_past_it_is_cut():
    assert jobs.ERROR_MAX == 300
    head = "RuntimeError: "
    exact = RuntimeError("x" * (jobs.ERROR_MAX - len(head)))
    assert jobs.describe_failure(exact) == head + "x" * (jobs.ERROR_MAX - len(head))
    over = jobs.describe_failure(RuntimeError("x" * (jobs.ERROR_MAX - len(head) + 1)))
    assert len(over) == jobs.ERROR_MAX and over.endswith("x…")
    assert jobs.describe_failure(RuntimeError()) == "RuntimeError"


def test_an_unexpected_error_is_shortened(monkeypatch):
    def build(ticker, fresh, fence=None):
        raise RuntimeError("x" * 5000)

    reg = _registry(build=build)
    done = _finish(reg, reg.start("KO"))
    assert done.error.startswith("RuntimeError: xxx") and len(done.error) <= jobs.ERROR_MAX


def test_an_interrupt_like_exit_still_finishes_the_job():
    def build(ticker, fresh, fence=None):
        raise SystemExit(3)  # a script-style exit inside the build

    reg = _registry(build=build)
    done = _finish(reg, reg.start("KO"))
    assert done.state == jobs.FAILED and done.error == "SystemExit: 3"


def test_the_registry_is_bounded_and_never_drops_a_running_job():
    gate, entered = threading.Event(), threading.Event()

    def build(ticker, fresh, fence=None):
        if ticker == "HOLD":
            entered.set()
            gate.wait(10)

    reg = _registry(build=build, max_jobs=5)
    held = reg.start("HOLD")
    assert entered.wait(10)
    started = []
    for i in range(12):
        job = reg.start(f"T{i}")
        started.append(job)
        _finish(reg, job)
    assert len(reg) == 5  # full, never under: only the excess is dropped
    assert reg.get(held.id) is not None and reg.get(held.id).state == jobs.RUNNING
    # The newest finished runs are the ones kept.
    assert [reg.get(j.id) is not None for j in started] == [False] * 8 + [True] * 4
    assert reg.latest("T0") is None and reg.latest("T11").state == jobs.DONE
    gate.set()
    _finish(reg, held)


def test_the_default_registry_is_bounded_at_fifty():
    assert jobs.MAX_JOBS == 50 and jobs.REGISTRY.max_jobs == 50


def test_active_lists_only_runs_in_flight():
    gate, entered = threading.Event(), threading.Event()

    def build(ticker, fresh, fence=None):
        if ticker == "KO":
            entered.set()
            gate.wait(10)

    reg = _registry(build=build)
    _finish(reg, reg.start("CRM"))
    ko = reg.start("KO")
    assert entered.wait(10)
    assert [j.ticker for j in reg.active()] == ["KO"]
    gate.set()
    _finish(reg, ko)
    assert reg.active() == []


def test_wait_on_an_unknown_job_is_none():
    assert _registry().wait("0" * 32, timeout=0.01) is None


def test_module_functions_use_the_default_registry(monkeypatch):
    reg = _registry()
    monkeypatch.setattr(jobs, "REGISTRY", reg)
    job = jobs.start("KO")
    assert jobs.wait(job.id, timeout=10).state == jobs.DONE
    assert jobs.latest("KO").id == job.id and jobs.active() == []


def test_again_while_running_starts_one_more_run_after_it():
    """A price recorded while a run is in flight: that run read the old
    observation as it started, so one more run follows it (and several
    asks are one more run, fresh if any asked for fresh)."""
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def build(ticker, fresh, fence=None):
        calls.append(fresh)
        if len(calls) == 1:
            entered.set()
            gate.wait(10)

    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered.wait(10)
    assert reg.start("KO", again=True).id == first.id
    assert reg.start("KO", fresh=True, again=True).id == first.id
    assert reg.start("KO", again=True).id == first.id
    gate.set()
    _finish(reg, first)
    follow = reg.latest("KO")
    assert follow.id != first.id
    _finish(reg, follow)
    assert calls == [False, True] and reg.latest("KO").id == follow.id


def test_without_again_a_start_while_running_adds_nothing():
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def build(ticker, fresh, fence=None):
        calls.append(ticker)
        entered.set()
        gate.wait(10)

    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered.wait(10)
    reg.start("KO")
    gate.set()
    _finish(reg, first)
    assert reg.latest("KO").id == first.id and calls == ["KO"]


def test_again_with_nothing_running_is_an_ordinary_start():
    calls = []
    reg = _registry(build=lambda t, f, fence=None: calls.append(f))
    _finish(reg, reg.start("KO", fresh=True, again=True))
    assert calls == [True] and len(reg) == 1


def test_a_follow_up_in_a_full_registry_still_wakes_the_waiter():
    """The follow-up start prunes finished runs; with room for one, it
    prunes the run that just finished, whose waiter must still be woken."""
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def build(ticker, fresh, fence=None):
        calls.append(ticker)
        if len(calls) == 1:
            entered.set()
            gate.wait(10)

    import time

    reg = _registry(build=build, max_jobs=1)
    first = reg.start("KO")
    assert entered.wait(10)
    reg.start("KO", again=True)
    woke: list[float] = []
    waiter = threading.Thread(target=lambda: (reg.wait(first.id, timeout=5),
                                              woke.append(time.monotonic())))
    waiter.start()  # holds the run's event before it is pruned
    released = time.monotonic()
    gate.set()
    waiter.join(10)
    assert woke and woke[0] - released < 4  # woken, not timed out
    follow = reg.latest("KO")
    assert follow is not None and follow.id != first.id
    assert reg.wait(follow.id, timeout=10).state == jobs.DONE
    assert len(reg) == 1 and calls == ["KO", "KO"]


# --- fix round 1 (independent review of 9d00328) ---------------------------------------


def _gated(n_hold: int = 1):
    """A build whose first ``n_hold`` calls wait for their own gate; the
    calls' ``fresh`` in order, and an event set as each held call enters."""
    gates = [threading.Event() for _ in range(n_hold)]
    entered = [threading.Event() for _ in range(n_hold)]
    seen: list[bool] = []

    def build(ticker, fresh, fence=None):
        i = len(seen)
        seen.append(fresh)
        if i < n_hold:
            entered[i].set()
            gates[i].wait(10)
        return f"{i:032x}"

    return build, gates, entered, seen


def test_refresh_while_a_cached_run_is_in_flight_follows_it_fresh(caplog):
    """Reviewer's jobsrepro.py #2: "Refresh from SEC" pressed while a cached
    run (say, the one a price POST started) is in flight was the cached run
    and nothing more, so the fresh fetch never happened. A fresh start now
    queues a fresh run after it; a second click is the same queued run."""
    build, gates, entered, seen = _gated()
    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered[0].wait(10)
    assert reg.start("KO", fresh=True).id == first.id
    assert reg.start("KO", fresh=True).id == first.id
    gates[0].set()
    _finish(reg, first)
    follow = reg.latest("KO")
    assert follow.id != first.id and follow.fresh
    _finish(reg, follow)
    assert seen == [False, True]
    assert not [r for r in caplog.records if "abandoned" in r.getMessage()]


def test_the_queued_follow_up_is_readable_for_the_status():
    build, gates, entered, _ = _gated()
    reg = _registry(build=build)
    assert reg.follow_up("KO") is None
    first = reg.start("KO")
    assert entered[0].wait(10)
    assert reg.follow_up("KO") is None
    reg.start("KO", again=True)
    assert reg.follow_up("KO") is False   # one more run, from the cache
    reg.start("KO", fresh=True)
    assert reg.follow_up("KO") is True    # ...made fresh by a refresh
    reg.start("KO", again=True)
    assert reg.follow_up("KO") is True    # a later cached ask never unmakes it
    gates[0].set()
    _finish(reg, first)
    _finish(reg, reg.latest("KO"))
    assert reg.follow_up("KO") is None
    assert jobs.follow_up("CRM") is None  # the module function reads the default registry


def test_a_fresh_start_while_a_fresh_run_is_in_flight_adds_nothing():
    build, gates, entered, seen = _gated()
    reg = _registry(build=build)
    first = reg.start("KO", fresh=True)
    assert entered[0].wait(10)
    assert reg.start("KO", fresh=True).id == first.id
    assert reg.follow_up("KO") is None
    gates[0].set()
    _finish(reg, first)
    assert seen == [True] and len(reg) == 1


def _clock(monkeypatch, start: datetime) -> list[datetime]:
    now = [start]
    monkeypatch.setattr(jobs, "_now", lambda: now[0])
    return now


def test_a_run_past_the_stall_limit_is_stalled_and_a_new_run_starts(monkeypatch, caplog):
    """Reviewer's jobsrepro.py #1: a build that never returns (a hung read,
    a stalled lock holder) kept its ticker "running" for the life of the
    server. Past ``STALL_AFTER_S`` it is shown as stalled and a start is a
    new run; the old thread is abandoned, and what it ends with is ignored."""
    assert jobs.STALL_AFTER_S == 600  # ten minutes: a build is 10-20 s, documents and all
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    build, gates, entered, seen = _gated(2)
    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered[0].wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S)
    assert not reg.latest("KO").stalled and reg.latest("KO").active
    assert reg.start("KO").id == first.id          # at the limit: still the run in flight
    now[0] += timedelta(seconds=1)
    held = reg.latest("KO")
    assert held.stalled and not held.active and held.state == jobs.RUNNING
    assert reg.active() == []
    second = reg.start("KO")
    assert second.id != first.id
    assert entered[1].wait(10)
    old = reg.get(first.id)
    assert old.state == jobs.STALLED and old.stalled and not old.active
    assert old.finished_at == now[0] and "stalled" in old.error
    assert str(jobs.STALL_AFTER_S // 60) in old.error
    # The abandoned thread ends: how it ended is recorded on its own run
    # (Hermes audit of PR #118, finding 1: whether it became live is the
    # publish's to decide, by its fence, not this registry's), and the new
    # run is untouched by it: still the ticker's, still running.
    gates[0].set()
    ended = reg.wait(first.id, timeout=10)
    assert ended.state == jobs.DONE and ended.generation_id == f"{0:032x}"
    assert ended.error is None
    assert reg.latest("KO").id == second.id and reg.latest("KO").state == jobs.RUNNING
    gates[1].set()
    done = _finish(reg, second)
    assert done.state == jobs.DONE and done.generation_id == f"{1:032x}"
    assert seen == [False, False]
    # The server log says the abandoned run ended, once; the new run's end
    # is an ordinary one.
    said = [r.getMessage() for r in caplog.records if "abandoned as stalled" in r.getMessage()]
    assert said == ["workbench run of KO ended after it was abandoned as stalled: done; it no "
                    "longer leads the ticker"]


def test_an_abandoned_run_never_takes_the_new_runs_follow_up(monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    build, gates, entered, seen = _gated(2)
    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered[0].wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    second = reg.start("KO")
    assert entered[1].wait(10)
    reg.start("KO", again=True)          # a price recorded during the new run
    gates[0].set()                       # the abandoned thread ends first
    reg.wait(first.id, timeout=10)
    assert reg.follow_up("KO") is False and reg.latest("KO").id == second.id
    gates[1].set()
    _finish(reg, second)
    follow = reg.latest("KO")
    assert follow.id not in (first.id, second.id)
    _finish(reg, follow)
    assert seen == [False, False, False]


def test_a_refresh_asked_of_a_stalled_run_is_the_new_runs(monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    build, gates, entered, seen = _gated(1)
    reg = _registry(build=build)
    first = reg.start("KO")
    assert entered[0].wait(10)
    reg.start("KO", fresh=True)          # queued behind the run that then stalls
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    second = reg.start("KO")             # the operator's Run, once it shows stalled
    assert second.fresh and reg.follow_up("KO") is None
    _finish(reg, second)
    gates[0].set()
    reg.wait(first.id, timeout=10)
    assert seen == [False, True] and reg.latest("KO").id == second.id


def test_a_stalled_run_is_pruned_like_a_finished_one(monkeypatch):
    now = _clock(monkeypatch, datetime(2026, 10, 9, 12, tzinfo=UTC))
    errors: list = []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    build, gates, entered, _ = _gated(1)
    reg = _registry(build=build, max_jobs=1)
    first = reg.start("KO")
    assert entered[0].wait(10)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    other = reg.start("CRM")
    _finish(reg, other)
    assert reg.get(first.id) is None and len(reg) == 1
    gates[0].set()                       # its thread ends with nothing to record into
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
            t.name == "workbench-KO" for t in threading.enumerate()):
        time.sleep(0.01)
    assert reg.get(first.id) is None and reg.latest("CRM").id == other.id
    assert errors == []


def test_a_publish_held_past_the_wait_fails_the_run_as_busy(tmp_path, monkeypatch):
    """Reviewer's jobsrepro.py #3: the build waited on the publish lock for
    as long as any holder held it, so `PublishBusy` could never reach a
    run. A workbench run publishes under ``reports/workbench/`` and waits
    for the lock at most `review.PUBLISH_WAIT_S` (the review console's
    wait), then fails as busy with nothing published."""
    from app.services.reporting.report_files import publish_lock

    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(review, "PUBLISH_WAIT_S", 0.3)
    seen: dict = {}

    def build(ticker, with_docs=True, report_day=None, fresh=False, out_dir=None,
              publish_timeout=None, **kw):
        seen.update(out_dir=out_dir, publish_timeout=publish_timeout)
        out = out_dir / f"{ticker}_{date.today().isoformat()}.md"
        with replacing(out, timeout=publish_timeout) as staged:
            staged.report.write_text("# card\n")
            staged.ledger.write_text("{}")
        return out, "no acute signals"

    monkeypatch.setattr(reporting, "build_report", build)
    report = tmp_path / "reports" / "workbench" / f"KO_{date.today().isoformat()}.md"
    report.parent.mkdir(parents=True)
    held, release = threading.Event(), threading.Event()

    def hold():
        with publish_lock(report):
            held.set()
            release.wait(20)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        reg = jobs.Registry()
        t0 = time.monotonic()
        done = reg.wait(reg.start("KO").id, timeout=10)
        waited = time.monotonic() - t0
    finally:
        release.set()
        holder.join(10)
    assert done.state == jobs.FAILED, done
    assert done.error == "another run is publishing this report; try again"
    assert waited < 5
    assert seen == {"out_dir": tmp_path / "reports" / "workbench", "publish_timeout": 0.3}
    assert read_live(report) is None
