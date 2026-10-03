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
observation" — so a report build fails closed on it. Nothing is read or
written through a symlink (the journal's rule, Hermes audit of 424b0b4,
finding 5).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, date
from pathlib import Path
from typing import Annotated

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

SOURCE_MAX = 200
NOTE_MAX = 500
SCENARIO_NAME_MAX = 60


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
    fcf_growth: Annotated[float, Field(gt=-1.0, allow_inf_nan=False)]
    years: Annotated[int, Field(ge=1, le=50)]
    terminal_growth: Annotated[float, Field(gt=-1.0, allow_inf_nan=False)] | None = None
    required_return: Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)] | None = None

    @field_validator("name")
    @classmethod
    def _named(cls, v: str) -> str:
        v = v.strip()
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
        v = v.strip()
        if not v:
            raise ValueError("source must say what was looked at (an exchange close, a "
                             "broker statement, a terminal screen)")
        return v

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

    def age_days(self, on: date) -> int:
        """Whole days from the observation's own (UTC) day to `on`."""
        return (on - self.observed_at.astimezone(UTC).date()).days

    def is_stale(self, on: date) -> bool:
        return self.age_days(on) > STALE_AFTER_DAYS


@dataclass(frozen=True)
class LoadedObservation:
    """An observation with the digest of what it was read from: the file's
    bytes (the ledger's `observation_sha256`), or the canonical JSON of an
    observation built in memory (`of`), which has no file."""

    observation: MarketObservation
    sha256: str
    path: Path | None

    @classmethod
    def of(cls, observation: MarketObservation) -> LoadedObservation:
        digest = hashlib.sha256(observation.model_dump_json().encode()).hexdigest()
        return cls(observation, digest, None)


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


def load_observation(path: Path) -> LoadedObservation:
    """The observation at `path`, with the digest of the bytes it was read
    from. Anything that is not exactly one observation — not a regular
    file, not UTF-8, not JSON, not valid — is an `ObservationError` naming
    the path."""
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
    return LoadedObservation(observation, hashlib.sha256(raw).hexdigest(), path)


def read_observation(path: Path) -> MarketObservation:
    return load_observation(path).observation


def find_observation(journal_root: Path, ticker: str) -> LoadedObservation | None:
    """The ticker's observation, or None when nothing is at its path. A file
    that is there but is not this ticker's valid observation raises: it is
    never read as "none recorded"."""
    path = observation_path(journal_root, ticker)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    loaded = load_observation(path)
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
