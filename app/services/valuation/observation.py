"""The market observation: one share price per ticker, supplied by the
operator and recorded once, with the exact timestamp and what was looked at.

v1 decision (the review of 02c2aac, accepted by the user): the price is an
OBSERVATION the operator records, never a fetch. There is no live price
source in the repo (`backtesting/prices.py` is a backtest-only client with
no provenance), and a number the engine fetched itself would be the one
line of the card with no human behind it. So the file under
``journal/market/<TICKER>.json`` holds the price, its timestamp (aware,
never in the future of when it was recorded), its source as free text, and
optionally the model assumptions and scenarios the expectations block
should use instead of its documented defaults.

Reading is strict: a file that is present but cannot be taken as an
observation is an `ObservationError` naming the path — never "no
observation" — so a report build fails closed on it. A file dated in the
future is refused the same way (review of 48b1f04, F2: observed_at ≤
recorded_at held with both in 2999, and the card carried a negative age).
Nothing is read or written through a symlink (the journal's rule, Hermes
audit of 424b0b4, finding 5).

Days are EDGAR's. A filing is dated by its US/Eastern calendar day, so the
observation's own day — the one its availability and age are counted on —
is its Eastern day too, not the UTC one (F5: 23:30 Eastern is the next day
in UTC, and the bridge was reading that day's 10-Q into the price).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.services.journal.store import safe_ticker
from app.services.reporting.report_files import own_dir, write_atomic

# An observation older than this on the report's day is marked STALE on the
# card: a week is longer than any filing-night cycle, and a price that old
# beside today's filing facts mixes two moments without saying so.
STALE_AFTER_DAYS = 7
MARKET_DIR = "market"
# EDGAR's filing calendar.
EASTERN = ZoneInfo("America/New_York")

SOURCE_MAX = 200
NOTE_MAX = 500
SCENARIO_NAME_MAX = 60

# The growth bracket the expectations block's bisection searches, and the
# bound on a scenario's growth: FCF collapsing 99% a year to doubling every
# year. A scenario past the top overflowed the present value to inf, which
# the card printed and the ledger could not hold (F3).
GROWTH_LOW = -0.99
GROWTH_HIGH = 1.0

# Anything that could break a line or hide in one: an operator's source,
# note or scenario name is emitted into the report, and a newline in it
# forged a heading there (F4). The C0 and C1 controls (Unicode category Cc)
# and the two separators Zl/Zp: NEL (U+0085), LINE SEPARATOR (U+2028) and
# PARAGRAPH SEPARATOR (U+2029) are line breaks to an editor and to some
# renderers, and were passing (review of f73b059, R1).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _plain_text(v: str, what: str) -> str:
    found = _CONTROL.search(v)
    if found:
        raise ValueError(f"{what} must not contain control characters or line separators "
                         f"(newlines, tabs, NEL, U+2028, U+2029, ...): found {found.group()!r}")
    return v


class ObservationError(ValueError):
    """A file that is at an observation's path but is not one, or is not a
    regular file. Carries the path; the message names it."""

    def __init__(self, path: Path | None, message: str) -> None:
        super().__init__(f"{path}: {message}" if path is not None else message)
        self.path = path


class Assumptions(BaseModel):
    """The expectations block's inputs: a required return, a growth rate
    beyond the horizon, and the horizon. The defaults are documented in
    docs/valuation_spec.md and labelled as defaults wherever they are used."""

    model_config = ConfigDict(frozen=True)

    required_return: Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)] = 0.09
    terminal_growth: Annotated[float, Field(gt=-1.0, allow_inf_nan=False)] = 0.025
    horizon_years: Annotated[int, Field(ge=1, le=50)] = 10

    @model_validator(mode="after")
    def _terminal_below_required(self) -> Assumptions:
        if self.terminal_growth >= self.required_return:
            raise ValueError(
                f"terminal_growth ({self.terminal_growth}) must be below required_return "
                f"({self.required_return}): the terminal value is otherwise unbounded"
            )
        return self


class Scenario(BaseModel):
    """A named FCF path the operator wants valued: constant growth for
    `years`, then `terminal_growth` (the assumptions' unless given) at
    `required_return` (likewise)."""

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=SCENARIO_NAME_MAX)]
    fcf_growth: Annotated[float, Field(gt=-1.0, le=GROWTH_HIGH, allow_inf_nan=False)]
    years: Annotated[int, Field(ge=1, le=50)]
    terminal_growth: Annotated[float, Field(gt=-1.0, allow_inf_nan=False)] | None = None
    required_return: Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)] | None = None

    @field_validator("name")
    @classmethod
    def _named(cls, v: str) -> str:
        v = _plain_text(v, "a scenario name").strip()
        if not v:
            raise ValueError("a scenario needs a name")
        return v

    @model_validator(mode="after")
    def _terminal_below_required(self) -> Scenario:
        if (self.terminal_growth is not None and self.required_return is not None
                and self.terminal_growth >= self.required_return):
            raise ValueError(f"scenario {self.name!r}: terminal_growth must be below "
                             "required_return")
        return self


class MarketObservation(BaseModel):
    """What the operator saw, when, and where."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    price: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "USD"
    observed_at: AwareDatetime
    source: Annotated[str, Field(max_length=SOURCE_MAX)]
    note: Annotated[str, Field(max_length=NOTE_MAX)] | None = None
    recorded_at: AwareDatetime
    assumptions: Assumptions | None = None
    scenarios: tuple[Scenario, ...] = ()

    @field_validator("ticker", mode="before")
    @classmethod
    def _ticker(cls, v: object) -> str:
        # The journal's rule: the file is named after it.
        return safe_ticker(v if isinstance(v, str) else "")

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = _plain_text(v, "source").strip()
        if not v:
            raise ValueError("source must say what was looked at (an exchange close, a "
                             "broker statement, a terminal screen)")
        return v

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return None if v is None else _plain_text(v, "note")

    @model_validator(mode="after")
    def _consistent(self) -> MarketObservation:
        if self.observed_at > self.recorded_at:
            raise ValueError(
                f"observed_at ({self.observed_at.isoformat()}) is after recorded_at "
                f"({self.recorded_at.isoformat()}): a price cannot be seen before it is recorded"
            )
        names = [s.name for s in self.scenarios]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            # Ledger rows are keyed on the name: two of one name would be one row.
            raise ValueError(f"scenario names must be distinct: {dupes}")
        return self

    @property
    def eastern_day(self) -> date:
        """The observation's US/Eastern calendar day — EDGAR's, on which a
        filing's availability to it and its age are both counted (F5)."""
        return self.observed_at.astimezone(EASTERN).date()

    def age_days(self, on: date) -> int:
        """Whole days from the observation's Eastern day to `on`, never
        negative: the reader refuses the future (F2), and a report dated
        before the observation's day by a clock's skew is age 0, not -1."""
        return max(0, (on - self.eastern_day).days)

    def is_stale(self, on: date) -> bool:
        return self.age_days(on) > STALE_AFTER_DAYS


def parse_observed_at(text: str, *, name: str = "observed_at") -> datetime:
    """When a price was observed, as the operator typed it: an ISO-8601
    time with its UTC offset. A naive time is refused, not assumed UTC.
    ``name`` is what the operator typed it into (``--at`` on the CLI).

    The one parser of a typed time, for `market.py record --at` and the
    workbench's price form alike (independent review of 9d00328): the form
    handed the text to the model, whose datetime coercion also reads a
    bare number as Unix seconds, so "0", "1700000000", or the price typed
    into the time box, recorded 1970 or 2023 where the CLI refused them."""
    try:
        at = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"{name} {text!r}: {e}") from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{name} {text!r} has no UTC offset: write the time as observed, with "
                         "its offset (e.g. 2026-11-18T16:00:00-05:00)")
    return at


def eastern_today(now: datetime | None = None) -> date:
    """The day an age is counted on: `now` (the clock unless given) on
    EDGAR's calendar. A report's `generated_on` is the host's local date —
    UTC on a server — and anchors the streams; counted on it, an evening
    observation read "age 1 day" on the card and 0 in `market.py show`
    (review of f73b059, R2). A naive `now` is refused, not assumed."""
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now has no UTC offset: an age is counted on an aware clock")
    return now.astimezone(EASTERN).date()


@dataclass(frozen=True)
class LoadedObservation:
    """An observation with what it was read from and its digest: the file's
    bytes (the ledger's `observation_sha256`), or the canonical JSON of an
    observation built in memory (`of`), which has no file. `raw` is kept so
    that what is shown is what was read (`market.py show`, F10): the digest
    is always of these bytes."""

    observation: MarketObservation
    sha256: str
    path: Path | None
    raw: bytes

    @classmethod
    def of(cls, observation: MarketObservation) -> LoadedObservation:
        raw = observation.model_dump_json().encode()
        return cls(observation, hashlib.sha256(raw).hexdigest(), None, raw)


def observation_path(journal_root: Path, ticker: str) -> Path:
    """``<journal>/market/<TICKER>.json``; the ticker is validated as the
    journal validates its file names (`ValueError` otherwise)."""
    return journal_root / MARKET_DIR / f"{safe_ticker(ticker)}.json"


def _read_regular(path: Path) -> bytes:
    """The file's bytes, if it is a regular file: a symlink is refused, not
    followed, and anything else at the name is refused too."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise ObservationError(path, "no such file") from None
    if stat.S_ISLNK(mode):
        raise ObservationError(path, "is a symlink; an observation is never read through one")
    if not stat.S_ISREG(mode):
        raise ObservationError(path, "is not a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _refuse_future(path: Path, observation: MarketObservation, now: datetime | None) -> None:
    """A price cannot have been seen, nor recorded, after `now` (F2). The
    model alone cannot know the time; the reader does."""
    now = now if now is not None else datetime.now(UTC)
    for name in ("observed_at", "recorded_at"):
        when = getattr(observation, name)
        if when > now:
            raise ObservationError(path, f"{name} {when.isoformat()} is in the future "
                                         f"(now {now.isoformat()})")


def load_observation(path: Path, *, now: datetime | None = None) -> LoadedObservation:
    """The observation at `path`, with the digest of the bytes it was read
    from. Anything that is not exactly one observation — not a regular
    file, not UTF-8, not JSON, not valid, dated after `now` (the clock
    unless given) — is an `ObservationError` naming the path."""
    raw = _read_regular(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ObservationError(path, f"not UTF-8 ({e})") from None
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ObservationError(path, f"not JSON ({e})") from None
    try:
        observation = MarketObservation.model_validate(doc)
    except ValidationError as e:
        raise ObservationError(path, f"not a market observation ({e})") from None
    _refuse_future(path, observation, now)
    return LoadedObservation(observation, hashlib.sha256(raw).hexdigest(), path, raw)


def read_observation(path: Path, *, now: datetime | None = None) -> MarketObservation:
    return load_observation(path, now=now).observation


def find_observation(
    journal_root: Path, ticker: str, *, now: datetime | None = None
) -> LoadedObservation | None:
    """The ticker's observation, or None when nothing is at its path. A file
    that is there but is not this ticker's valid observation raises: it is
    never read as "none recorded"."""
    path = observation_path(journal_root, ticker)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    loaded = load_observation(path, now=now)
    if loaded.observation.ticker != safe_ticker(ticker):
        raise ObservationError(
            path, f"the observation names {loaded.observation.ticker}, not {safe_ticker(ticker)}")
    return loaded


def _refuse_link(path: Path) -> None:
    if path.is_symlink():
        raise OSError(errno.ELOOP, f"{path} is a symlink; it is never followed")


def write_observation(journal_root: Path, observation: MarketObservation) -> Path:
    """Write the observation whole (`report_files.write_atomic`) under
    ``<journal>/market/``, which is created when absent and refused when it
    is a symlink (`own_dir`); a link at the file's own name is refused too.
    ASCII JSON: readable whatever the locale of the next reader."""
    market = journal_root / MARKET_DIR
    market.mkdir(parents=True, exist_ok=True)
    own_dir(market)
    path = observation_path(journal_root, observation.ticker)
    _refuse_link(path)
    text = json.dumps(observation.model_dump(mode="json"), indent=1, sort_keys=True,
                      ensure_ascii=True) + "\n"
    write_atomic(path, text)
    return path


def remove_observation(journal_root: Path, ticker: str) -> Path | None:
    """Remove the ticker's observation; its path, or None when there was
    none. A symlink at the name is refused, not removed: it is not ours."""
    path = observation_path(journal_root, ticker)
    _refuse_link(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path
