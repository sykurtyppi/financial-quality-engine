"""Report runs started from the workbench: one per ticker, in a thread.

A build takes ~10-20 s of SEC fetches, too long for a request to wait on, so
"Run" starts `reporting.build_report` — the full publish path: report,
ledger, generation, fail-closed — in a daemon thread and the page polls its
state. In-process only: a server restart forgets its runs (the reports they
published stay; the pages read those, not this registry), and two processes
publishing one report are serialized by `report_files.publish_lock`, not
here.

A run publishes under ``reports/workbench/`` (`views.reports_dir`), never at
a journal case's live name, and waits for the publish lock at most
`review.PUBLISH_WAIT_S` (then fails as busy). A run that has not ended
after `STALL_AFTER_S` is stalled: a thread cannot be stopped, so it is
abandoned — the next start is a new run, and the old one no longer leads
the ticker (independent review of 9d00328).

Which run is live is decided on disk, not here (Hermes audit of PR #118,
finding 1): ignoring an abandoned run's result in this registry did not stop
its publish, which had already switched the live names when it returned.
Each run asked for takes the ticker's next request number
(`fencing.request`, a counter file shared by every process on the reports
folder) before its thread starts; the publish seals it and never makes a
run live once one asked for later has published, on any day's report
(the ticker's high-water mark; `report_files.Superseded`): such a run
ends `SUPERSEDED`, its report kept in the history. A counter that cannot be
read fails the run at once, with nothing built.

What the page shows of a run is a `Job`, a frozen snapshot taken under the
registry's lock, so a template never reads a run half-updated. A failure is
one sentence the operator can act on (`describe_failure`); the traceback
goes to the server log, never into the page.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.services.ingestion.sec_client import SecClientError
from app.services.journal import reporting, review, store
from app.services.reporting.report_files import (
    NotPublished,
    PublishBusy,
    PublishInDoubt,
    Superseded,
    recording,
)
from app.services.workbench import fencing, views

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
# A run abandoned as stalled (see STALL_AFTER_S).
STALLED = "stalled"
# A run that finished after a run asked for later was live: kept in the
# history, never made live (`report_files.Superseded`).
SUPERSEDED = "superseded"
# Seconds a run may go without ending before it is stalled: a build is
# 10-20 s, documents and all, and its SEC reads time out and retry within a
# minute or two; ten minutes is a hang (a read that never returns, a lock
# holder that never lets go), not a slow filing night.
STALL_AFTER_S = 600
# Kept runs (finished ones beyond this are dropped oldest first; a run in
# flight never is): a server left up for a season stays small.
MAX_JOBS = 50
# An unexpected error's message is cut here: the page says what kind of
# failure it was, the server log has the rest.
ERROR_MAX = 300

log = logging.getLogger(__name__)

# (ticker, fresh, fence) -> the generation id the run published, or None.
Build = Callable[[str, bool, int | None], str | None]
# ticker -> the run's request number (`fencing.request`).
FenceSource = Callable[[str], int]


@dataclass(frozen=True)
class Job:
    id: str
    ticker: str
    fresh: bool
    state: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    generation_id: str | None = None
    # The run's request number, sealed into what it publishes; None for a
    # registry that does not fence (a test's stand-in build).
    fence: int | None = None

    @property
    def stalled(self) -> bool:
        """Abandoned as stalled, or in flight for over `STALL_AFTER_S`."""
        if self.state == STALLED:
            return True
        since = self.started_at or self.created_at
        return (self.state in (QUEUED, RUNNING)
                and _now() - since > timedelta(seconds=STALL_AFTER_S))

    @property
    def active(self) -> bool:
        """In flight and not stalled: what a start joins rather than repeats."""
        return self.state in (QUEUED, RUNNING) and not self.stalled


def _now() -> datetime:
    return datetime.now(UTC)


def describe_failure(e: BaseException) -> str:
    """Why a run failed, in the words the operator acts on.

    A publish IN DOUBT is said whole: its message ends with the command
    that puts the earlier run back, and is never shortened. A missing
    EDGAR_IDENTITY is a setup step, not an SEC outage."""
    if isinstance(e, PublishInDoubt):
        return f"Report publish IN DOUBT: {e}"
    if isinstance(e, SecClientError):
        if not os.environ.get("EDGAR_IDENTITY"):
            return ("Setup: EDGAR_IDENTITY is not set. SEC requires a name and an email on "
                    'every request; export EDGAR_IDENTITY="Your Name you@example.com" and '
                    "restart the workbench.")
        return f"SEC fetch failed: {e}"
    if isinstance(e, PublishBusy):
        return "another run is publishing this report; try again"
    if isinstance(e, NotPublished):
        return str(e)
    text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return text if len(text) <= ERROR_MAX else text[: ERROR_MAX - 1] + "…"


def _build(ticker: str, fresh: bool, fence: int | None) -> str | None:
    """The CLI's build (`reporting.build_report`, looked up at call time),
    with documents, dated today, published under ``reports/workbench/``,
    waiting for the publish lock at most `review.PUBLISH_WAIT_S` (the review
    console's wait: a publish holds it for a moment), fenced with the run's
    request number and its ticker's high-water mark (`fencing.fence`; the
    ticker's lock is waited for as long). Returns the generation THIS run published
    (`report_files.recording`), never one read back from the live name,
    which another run may have taken meanwhile."""
    fenced = None if fence is None else fencing.fence(ticker, fence)
    with recording() as published:
        reporting.build_report(ticker, with_docs=True, fresh=fresh,
                               out_dir=views.reports_dir(),
                               publish_timeout=review.PUBLISH_WAIT_S, fence=fenced)
    return published[-1].generation_id if published else None


def _request_fence(ticker: str) -> int:
    """`fencing.request`, looked up at call time."""
    return fencing.request(ticker)


class Registry:
    """The runs of one server process, bounded and thread-safe."""

    def __init__(self, max_jobs: int = MAX_JOBS, build: Build | None = None,
                 fence: FenceSource | None = _request_fence) -> None:
        self.max_jobs = max_jobs
        self._build = build
        # Where each run's request number comes from; None: runs are not
        # fenced (a stand-in build that publishes nothing).
        self._fence = fence
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._done: dict[str, threading.Event] = {}
        # ticker -> fresh: a run asked for while one was in flight, started
        # when it ends (`start(..., again=True)`, or a fresh start behind a
        # cached run).
        self._again: dict[str, bool] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def start(self, ticker: str, *, fresh: bool = False, again: bool = False) -> Job:
        """Start a run of ``ticker`` (validated as the journal validates its
        file names; ValueError otherwise). While one is in flight for the
        ticker, that run is returned and nothing else starts: a double
        click is one build, not two publishes in a row.

        ``again``: the caller changed an input the run in flight has already
        read (a price recorded or removed: the build reads the observation
        as it starts), so one more run starts when it ends; several such
        asks while it runs are one more run. A ``fresh`` start behind a run
        from the cache is such an ask too ("Refresh from SEC" pressed while
        the run a price started is in flight): the cached run is not the
        fetch asked for, so a fresh one follows it (review of 9d00328).

        A run in flight past `STALL_AFTER_S` is abandoned here (`STALLED`)
        and a new one starts, taking over what was asked to follow it.

        A new run takes its request number here, under the registry's lock
        and before its thread starts, so runs are numbered in the order they
        were asked for (a run joined is not asked for again). A number that
        cannot be taken (`fencing.EpochError`) is the run's failure, said at
        once, and nothing is built."""
        t = store.safe_ticker(ticker)
        with self._lock:
            running = self._latest(t)
            if running is not None and running.active:
                if again or (fresh and not running.fresh):
                    self._again[t] = self._again.get(t, False) or fresh
                return running
            if running is not None and running.state in (QUEUED, RUNNING):
                self._jobs[running.id] = dataclasses.replace(
                    running, state=STALLED, finished_at=_now(),
                    error=(f"no result after {STALL_AFTER_S // 60} minutes: abandoned as "
                           "stalled (a hung SEC read, or a publish lock held elsewhere). "
                           "If it ever ends, it cannot replace a run asked for after it."))
                # The new run reads every input as it starts: it is the run
                # that was to follow, and fresh if that one was to be.
                fresh = self._again.pop(t, False) or fresh
            fence: int | None = None
            refused: str | None = None
            if self._fence is not None:
                try:
                    fence = self._fence(t)
                except (fencing.EpochError, OSError) as e:
                    refused = str(e)
            job = Job(uuid.uuid4().hex, t, fresh, QUEUED, _now(), fence=fence)
            if refused is not None:
                job = dataclasses.replace(job, state=FAILED, finished_at=job.created_at,
                                          error=refused)
            self._jobs[job.id] = job
            self._done[job.id] = done = threading.Event()
            self._prune()
        if refused is not None:
            done.set()
            return job
        threading.Thread(target=self._run, args=(job.id,), daemon=True,
                         name=f"workbench-{t}").start()
        return job

    def _run(self, job_id: str) -> None:
        # Held from the start: once this run is finished, a later start (its
        # own follow-up included) may prune it from the registry, event and
        # all, and a waiter on it must still be woken.
        with self._lock:
            done = self._done[job_id]
            job = self._jobs[job_id] = dataclasses.replace(
                self._jobs[job_id], state=RUNNING, started_at=_now())
        build = self._build or _build
        outcome: dict[str, object]
        try:
            try:
                gid = build(job.ticker, job.fresh, job.fence)
            except Superseded as e:
                # Not a failure: the run asked for later is live, and this
                # one's report is kept in the history (its generation).
                log.info("workbench run of %s superseded: %s", job.ticker, e)
                outcome = {"state": SUPERSEDED, "generation_id": e.generation_id, "error": None}
            except BaseException as e:  # noqa: BLE001 - a thread's error is the job's state
                # Its traceback is the server log's; the page gets one line.
                log.exception("workbench run of %s failed", job.ticker)
                outcome = {"state": FAILED, "error": describe_failure(e)}
            else:
                outcome = {"state": DONE, "generation_id": gid, "error": None}
            with self._lock:
                current = self._jobs.get(job_id)
                # Abandoned as stalled (and maybe pruned since): a newer run
                # is the ticker's now, and so is what was asked to follow it.
                # How it ended is still recorded (superseded, once the newer
                # run is live: the publish decides that, not this registry).
                abandoned = current is None or current.state == STALLED
                if current is not None:
                    self._jobs[job_id] = dataclasses.replace(
                        current, finished_at=_now(), **outcome)  # type: ignore[arg-type]
                again = None if abandoned else self._again.pop(job.ticker, None)
            if abandoned:
                log.warning("workbench run of %s ended after it was abandoned as stalled: "
                            "%s; it no longer leads the ticker", job.ticker, outcome["state"])
            if again is not None:
                self.start(job.ticker, fresh=again)
        finally:
            done.set()

    def _latest(self, ticker: str) -> Job | None:
        """The ticker's newest run; the caller holds the lock."""
        return next((j for j in reversed(self._jobs.values()) if j.ticker == ticker), None)

    def _prune(self) -> None:
        """Drop finished runs, oldest first, down to ``max_jobs``; the
        caller holds the lock. A run in flight is never dropped: its thread
        still has to record how it ended."""
        excess = len(self._jobs) - self.max_jobs
        for job_id in [j.id for j in self._jobs.values() if not j.active]:
            if excess <= 0:
                break
            del self._jobs[job_id]
            self._done.pop(job_id, None)
            excess -= 1

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self, ticker: str) -> Job | None:
        with self._lock:
            return self._latest(ticker)

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.active]

    def follow_up(self, ticker: str) -> bool | None:
        """The run queued to follow ``ticker``'s run in flight: True fresh,
        False from the cache, None when none is."""
        t = store.safe_ticker(ticker)
        with self._lock:
            return self._again.get(t)

    def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        """The run once it has finished (or as it is at ``timeout``); None
        for a run this registry does not hold."""
        with self._lock:
            done = self._done.get(job_id)
        if done is None:
            return None
        done.wait(timeout)
        return self.get(job_id)


REGISTRY = Registry()


def start(ticker: str, *, fresh: bool = False, again: bool = False) -> Job:
    return REGISTRY.start(ticker, fresh=fresh, again=again)


def latest(ticker: str) -> Job | None:
    return REGISTRY.latest(ticker)


def active() -> list[Job]:
    return REGISTRY.active()


def follow_up(ticker: str) -> bool | None:
    return REGISTRY.follow_up(ticker)


def wait(job_id: str, timeout: float | None = None) -> Job | None:
    return REGISTRY.wait(job_id, timeout)
