"""Each workbench run's request number: the fence that keeps a run asked for
earlier from replacing one asked for later.

Hermes audit of PR #118 @ 3983f8a, finding 1 (blocker): a run that stalled
was abandoned in the job registry, but its thread could not be stopped, and
when it ended — after the run that replaced it had published — its publish
switched the live names to the older run, while the page said the newer one
was done. Two servers on one reports folder raced the same way with no
registry in common.

So the order of requests is kept on disk: ``reports/workbench/.epochs/<T>``
holds the ticker's last request number, taken and incremented under an
``flock`` sidecar (``<T>.lock``, opened ``O_NOFOLLOW`` as `publish_lock` and
the watchlist's lock open theirs) when a run is asked for, before its
thread starts. The run hands it to `reporting.build_report(fence=)`, the
publish seals it into the ledger and compares it with the live run's
(`report_files.replacing`): a lower one is never made live.

A counter that cannot be read as one whole number fails the request closed
(`EpochError`, said on the page): it is never reset to 0, which could give
the next run a number below a live run's and refuse every run after it. A
counter that is missing (a first run; the folder copied without its hidden
files) starts above every kept run of the ticker, for the same reason.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services.journal import store
from app.services.reporting import report_files
from app.services.workbench import views

EPOCHS_DIR = ".epochs"
# One whole number in ASCII digits, as `request` writes it: "07", "٣", a
# sign or a second line is not a counter this engine wrote.
_COUNTER_RE = re.compile(r"(?:0|[1-9][0-9]*)\n?", re.ASCII)


class EpochError(RuntimeError):
    """The request counter cannot be read or written: no run is started,
    and the message (one sentence the page shows) says what to do."""


def _folder() -> Path:
    try:
        return report_files.own_dir(views.reports_dir() / EPOCHS_DIR)
    except OSError as e:
        raise EpochError(f"{e}: no run started") from e


def _refused(counter: Path, why: str) -> EpochError:
    return EpochError(
        f"{counter} is not a run counter ({why}): no run started. It holds the ticker's last "
        "run request as one whole number and is never reset (an older run could then "
        f"replace a newer one). Remove it to have the next run start above every kept run "
        f"of {counter.name}.")


def _read(counter: Path) -> int | None:
    """The counter's number; None when there is no counter."""
    try:
        fd = os.open(counter, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        why = "a symlink, never followed" if e.errno == errno.ELOOP else e.strerror or str(e)
        raise _refused(counter, why) from e
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(64)
    except OSError as e:
        raise _refused(counter, e.strerror or str(e)) from e
    # latin-1 reads any byte; only ASCII digits match the pattern.
    text = raw.decode("latin-1")
    if not _COUNTER_RE.fullmatch(text):
        raise _refused(counter, f"it holds {raw[:20]!r}")
    return int(text)


def _kept_fences(ticker: str) -> int:
    """The highest fence among the ticker's kept runs (0 for none). Runs
    that cannot be listed fail the request: starting below one of them
    could refuse every run after it."""
    problems: list[str] = []
    refs = views.runs(ticker, problems)
    if problems:
        raise EpochError(f"no run started: the request counter of {ticker} is missing and "
                         f"its kept runs cannot all be read to start it above them "
                         f"({problems[0]})")
    fences = [report_files.fence_of(report_files.live_name(r.report), r.report.parent)
              for r in refs if r.name is not None]
    return max((f for f in fences if f is not None), default=0)


@contextmanager
def _locked(folder: Path, ticker: str) -> Iterator[None]:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / f"{ticker}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        raise EpochError(f"the request counter's lock cannot be opened ({e}): no run started") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def request(ticker: str) -> int:
    """Take the ticker's next request number (1 for the first run): what a
    run asked for now is fenced with. Raises `EpochError` when the counter
    cannot be read or written; ValueError for a name that is not a ticker."""
    t = store.safe_ticker(ticker)
    folder = _folder()
    with _locked(folder, t):
        counter = folder / t
        last = _read(counter)
        n = (last if last is not None else _kept_fences(t)) + 1
        try:
            report_files.write_atomic(counter, f"{n}\n")
        except OSError as e:
            raise EpochError(f"{counter} cannot be written ({e}): no run started") from e
    return n


def current(ticker: str) -> int:
    """The ticker's last request number, 0 before any (read, not taken)."""
    t = store.safe_ticker(ticker)
    last = _read(_folder() / t)
    return 0 if last is None else last
