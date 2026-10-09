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

Which run may publish is decided by the ticker's high-water mark,
``.epochs/<T>.published``: the highest fence ever PUBLISHED for it, on any
day's report, read and raised under the same lock around the switch
(`report_files.Fence`). The mark, not one day's live run, is what an older
request is compared with (review of 2cbba1c: a run asked for before
midnight published after it on a new day's report, where nothing was live,
and became the card; a restore or an unfenced publish took the live run's
fence away). A newer request that failed never raised it, so an older run
may still publish then.

A counter that cannot be read as one whole number fails the request closed
(`EpochError`, said on the page): it is never reset to 0. Nor is it trusted
below what it must exceed: each request takes one more than the highest of
the counter, every kept run's fence and the high-water mark, so a counter
that is missing (a first run; the folder copied without its hidden files)
or behind (restored from an older backup) never hands out a number every
run after it would be superseded by. The lock is waited for at most
`review.PUBLISH_WAIT_S`: it is taken under the job registry's lock, and a
holder that never lets go must not freeze every page.
"""

from __future__ import annotations

import errno
from pathlib import Path

from app.services.journal import review, store
from app.services.reporting import report_files
from app.services.workbench import views

EPOCHS_DIR = ".epochs"
# Beside the counter `<T>`: the ticker's high-water mark of published fences.
MARK_SUFFIX = ".published"


class EpochError(RuntimeError):
    """The request counter, the high-water mark or their lock cannot be
    read or written: no run is started, and the message (one sentence the
    page shows) says what to do."""


def _folder() -> Path:
    try:
        return report_files.own_dir(views.reports_dir() / EPOCHS_DIR)
    except OSError as e:
        raise EpochError(f"{e}: no run started") from e


def _refused(path: Path, why: str, ticker: str) -> EpochError:
    return EpochError(
        f"{path} is not a run counter ({why}): no run started. It holds one whole number "
        "and is never reset (an older run could then replace a newer one). Remove it to "
        f"have the next run start above every kept and published run of {ticker}.")


def _read(path: Path, ticker: str) -> int | None:
    """The number in the counter or the mark at ``path``; None when there
    is none. Unreadable, a symlink or not one whole number: `EpochError`."""
    try:
        return report_files.read_count(path)
    except OSError as e:
        why = "a symlink, never followed" if e.errno == errno.ELOOP else e.strerror or str(e)
        raise _refused(path, why, ticker) from e
    except ValueError as e:
        raise _refused(path, str(e).removeprefix(f"{path} "), ticker) from e


def _kept_fences(ticker: str) -> int:
    """The highest fence among the ticker's kept runs (0 for none). Runs
    that cannot be listed or read fail the request: starting below one of
    them could refuse every run after it."""
    problems: list[str] = []
    refs = views.runs(ticker, problems)
    if problems:
        raise EpochError(f"no run started: the kept runs of {ticker} cannot all be read to "
                         f"number the next one above them ({problems[0]})")
    try:
        fences = [report_files.fence_of(report_files.live_name(r.report), r.report.parent)
                  for r in refs if r.name is not None]
    except OSError as e:
        raise EpochError(f"no run started: a kept run of {ticker} cannot be read to number "
                         f"the next one above it ({e})") from e
    return max((f for f in fences if f is not None), default=0)


def fence(ticker: str, number: int) -> report_files.Fence:
    """Request ``number`` of ``ticker``, as a run publishes with it: the
    ticker's high-water mark and lock beside its counter."""
    t = store.safe_ticker(ticker)
    folder = _folder()
    return report_files.Fence(number, folder / f"{t}{MARK_SUFFIX}", folder / f"{t}.lock")


def request(ticker: str) -> int:
    """Take the ticker's next request number (1 for the first run): what a
    run asked for now is fenced with. One more than the highest of the
    counter, the kept runs' fences and the high-water mark. Raises
    `EpochError` when any of them cannot be read, the counter cannot be
    written, or the lock is held past `review.PUBLISH_WAIT_S`; ValueError
    for a name that is not a ticker."""
    t = store.safe_ticker(ticker)
    f = fence(t, 0)
    counter = f.lock.parent / t
    try:
        with report_files.sidecar_lock(f.lock, timeout=review.PUBLISH_WAIT_S,
                                       busy=f"the request counter's lock of {t}"):
            n = max(_read(counter, t) or 0, _kept_fences(t), _read(f.mark, t) or 0) + 1
            try:
                report_files.write_count(counter, n)
            except OSError as e:
                raise EpochError(f"{counter} cannot be written ({e}): no run started") from e
    except report_files.PublishBusy as e:
        raise EpochError(f"{e} (another workbench process stuck?): no run started") from e
    except OSError as e:
        raise EpochError(f"the request counter's lock cannot be opened ({e}): no run "
                         "started") from e
    return n


def current(ticker: str) -> int:
    """The ticker's last request number, 0 before any (read, not taken)."""
    t = store.safe_ticker(ticker)
    last = _read(_folder() / t, t)
    return 0 if last is None else last
