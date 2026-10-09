"""Report runs started from the workbench: one per ticker, in a thread.

A build takes ~10-20 s of SEC fetches, too long for a request to wait on, so
"Run" starts `reporting.build_report` — the full publish path: report,
ledger, generation, fail-closed — in a daemon thread and the page polls its
state. In-process only: a server restart forgets its runs (the reports they
published stay; the pages read those, not this registry), and two processes
publishing one report are serialized by `report_files.publish_lock`, not
here.

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
from datetime import UTC, datetime

from app.services.ingestion.sec_client import SecClientError
from app.services.journal import reporting, store
from app.services.reporting.report_files import (
    NotPublished,
    PublishBusy,
    PublishInDoubt,
    recording,
)

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
# Kept runs (finished ones beyond this are dropped oldest first; a run in
# flight never is): a server left up for a season stays small.
MAX_JOBS = 50
# An unexpected error's message is cut here: the page says what kind of
# failure it was, the server log has the rest.
ERROR_MAX = 300

log = logging.getLogger(__name__)

# (ticker, fresh) -> the generation id the run published, or None.
Build = Callable[[str, bool], str | None]


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

    @property
    def active(self) -> bool:
        return self.state in (QUEUED, RUNNING)


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


def _build(ticker: str, fresh: bool) -> str | None:
    """The CLI's build (`reporting.build_report`, looked up at call time),
    with documents, dated today. Returns the generation THIS run published
    (`report_files.recording`), never one read back from the live name,
    which another run may have taken meanwhile."""
    with recording() as published:
        reporting.build_report(ticker, with_docs=True, fresh=fresh)
    return published[-1].generation_id if published else None


class Registry:
    """The runs of one server process, bounded and thread-safe."""

    def __init__(self, max_jobs: int = MAX_JOBS, build: Build | None = None) -> None:
        self.max_jobs = max_jobs
        self._build = build
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._done: dict[str, threading.Event] = {}
        # ticker -> fresh: a run asked for while one was in flight, started
        # when it ends (`start(..., again=True)`).
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
        asks while it runs are one more run."""
        t = store.safe_ticker(ticker)
        with self._lock:
            running = self._latest(t)
            if running is not None and running.active:
                if again:
                    self._again[t] = self._again.get(t, False) or fresh
                return running
            job = Job(uuid.uuid4().hex, t, fresh, QUEUED, _now())
            self._jobs[job.id] = job
            self._done[job.id] = threading.Event()
            self._prune()
        threading.Thread(target=self._run, args=(job.id,), daemon=True,
                         name=f"workbench-{t}").start()
        return job

    def _run(self, job_id: str) -> None:
        # Held from the start: once this run is finished, a later start (its
        # own follow-up included) may prune it from the registry, event and
        # all, and a waiter on it must still be woken.
        with self._lock:
            done = self._done[job_id]
        job = self._update(job_id, state=RUNNING, started_at=_now())
        build = self._build or _build
        try:
            gid = build(job.ticker, job.fresh)
        except BaseException as e:  # noqa: BLE001 - a thread's error is the job's state
            # Its traceback is the server log's; the page gets one line.
            log.exception("workbench run of %s failed", job.ticker)
            self._update(job_id, state=FAILED, finished_at=_now(), error=describe_failure(e))
        else:
            self._update(job_id, state=DONE, finished_at=_now(), generation_id=gid)
        finally:
            try:
                with self._lock:
                    again = self._again.pop(job.ticker, None)
                if again is not None:
                    self.start(job.ticker, fresh=again)
            finally:
                done.set()

    def _update(self, job_id: str, **changes: object) -> Job:
        with self._lock:
            job = dataclasses.replace(self._jobs[job_id], **changes)  # type: ignore[arg-type]
            self._jobs[job_id] = job
            return job

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


def wait(job_id: str, timeout: float | None = None) -> Job | None:
    return REGISTRY.wait(job_id, timeout)
