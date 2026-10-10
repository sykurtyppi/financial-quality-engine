"""The market observation: the operator's one price per ticker, recorded with
its exact timestamp and what was looked at (Hermes review of 02c2aac,
valuation plane). It is the only market datum the engine holds, so it is
validated hard on the way in and never read through a symlink."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.services.valuation.observation import (
    EASTERN,
    GROWTH_HIGH,
    STALE_AFTER_DAYS,
    Assumptions,
    LoadedObservation,
    MarketObservation,
    ObservationError,
    Scenario,
    eastern_today,
    find_observation,
    load_observation,
    observation_path,
    read_observation,
    remove_observation,
    write_observation,
)

NOW = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)


def _obs(**kw) -> MarketObservation:
    base = dict(
        ticker="KO", price=65.5, currency="USD", observed_at=NOW - timedelta(hours=1),
        source="NYSE official close (broker statement)", recorded_at=NOW,
    )
    return MarketObservation(**{**base, **kw})


def test_round_trip_write_read(tmp_path):
    obs = _obs(
        note="after the print",
        assumptions=Assumptions(required_return=0.1, terminal_growth=0.02, horizon_years=8),
        scenarios=(Scenario(name="bull", fcf_growth=0.1, years=5),
                   Scenario(name="bear", fcf_growth=-0.05, years=3, terminal_growth=0.0,
                            required_return=0.12)),
    )
    path = write_observation(tmp_path, obs)
    assert path == observation_path(tmp_path, "ko") == tmp_path / "market" / "KO.json"
    assert read_observation(path) == obs
    loaded = load_observation(path)
    assert loaded.observation == obs and loaded.path == path
    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    # The bytes read are kept with their digest (`market.py show` prints
    # them rather than reading the file a second time, F10).
    assert loaded.raw == path.read_bytes()
    assert find_observation(tmp_path, "KO") == loaded
    assert find_observation(tmp_path, "AAPL") is None
    # Only the observation itself is on disk: no temporary beside it.
    assert sorted(p.name for p in path.parent.iterdir()) == ["KO.json"]


def test_loaded_of_an_in_memory_observation_hashes_its_canonical_json():
    obs = _obs()
    loaded = LoadedObservation.of(obs)
    assert loaded.path is None and loaded.observation == obs
    assert loaded.raw == obs.model_dump_json().encode()
    assert loaded.sha256 == hashlib.sha256(loaded.raw).hexdigest()


@pytest.mark.parametrize("bad", [
    dict(observed_at=datetime(2026, 10, 3, 14)),              # naive
    dict(recorded_at=datetime(2026, 10, 3, 15)),              # naive
    dict(observed_at=NOW + timedelta(seconds=1)),             # after recorded_at
    dict(price=0.0), dict(price=-1.0), dict(price=float("nan")), dict(price=float("inf")),
    dict(currency="usd"), dict(currency="US"), dict(currency="USDX"), dict(currency="€UR"),
    dict(ticker="../KO"), dict(ticker=""), dict(ticker="K O"),
    dict(source=""), dict(source="   "), dict(source="x" * 201),
    dict(note="n" * 501),
    # Control characters (review of 48b1f04, F4): a newline in the source
    # would let the operator file write a heading into the report.
    dict(source="NYSE close\n## Decision card"), dict(source="tab\there"),
    dict(source="nul\x00"), dict(source="del\x7f"), dict(source="\x1b[31mred"),
    dict(note="line one\nline two"), dict(note="\r"),
])
def test_invalid_observations_are_refused(bad):
    with pytest.raises(ValidationError):
        _obs(**bad)


def test_control_characters_are_named_in_the_refusal():
    with pytest.raises(ValidationError, match="control character"):
        _obs(source="x\n## y")
    with pytest.raises(ValidationError, match="control character"):
        Scenario(name="a\nb", fcf_growth=0.05, years=5)
    # Ordinary punctuation and non-ASCII text are not control characters.
    assert _obs(source="Börse Frankfurt | Xetra close (€)").source == "Börse Frankfurt | Xetra close (€)"


# NEL, LINE SEPARATOR and PARAGRAPH SEPARATOR (review of f73b059, R1): not
# in the ASCII control range, but each breaks a line in an editor and in
# some renderers, so a source holding one could still start a heading in
# the report. Refused like a newline, in every operator-written field.
@pytest.mark.parametrize("sep", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("field", ["source", "note", "scenario name"])
def test_unicode_line_breaks_are_refused_in_every_text_field(sep, field):
    text = f"x{sep}## heading"
    with pytest.raises(ValidationError, match="control character"):
        if field == "scenario name":
            Scenario(name=text, fcf_growth=0.05, years=5)
        else:
            _obs(**{field: text})


def test_observed_at_may_equal_recorded_at():
    assert _obs(observed_at=NOW, recorded_at=NOW).observed_at == NOW


def test_ticker_follows_the_journal_rules():
    assert _obs(ticker=" ko ").ticker == "KO"
    assert _obs(ticker="brk.b").ticker == "BRK.B"


@pytest.mark.parametrize("bad", [
    dict(required_return=0.0), dict(required_return=1.0), dict(required_return=-0.1),
    dict(terminal_growth=0.09), dict(terminal_growth=0.2),   # not below the required return
    dict(terminal_growth=-1.5),
    dict(horizon_years=0), dict(horizon_years=51),
])
def test_assumption_bounds(bad):
    with pytest.raises(ValidationError):
        Assumptions(**bad)


def test_scenario_names_must_be_distinct():
    with pytest.raises(ValidationError, match="distinct"):
        _obs(scenarios=(Scenario(name="bull", fcf_growth=0.1, years=5),
                        Scenario(name="bull", fcf_growth=0.2, years=5)))


def test_default_assumptions_are_the_documented_ones():
    a = Assumptions()
    assert (a.required_return, a.terminal_growth, a.horizon_years) == (0.09, 0.025, 10)


@pytest.mark.parametrize("bad", [
    dict(name=""), dict(name="x" * 61), dict(years=0), dict(years=51),
    dict(fcf_growth=-1.0), dict(fcf_growth=float("nan")),
    # Growth above the bisection bracket (review of 48b1f04, F3): 1e300
    # overflowed the present value to inf, which the ledger could not hold.
    dict(fcf_growth=1e300), dict(fcf_growth=GROWTH_HIGH + 1e-9), dict(fcf_growth=float("inf")),
    dict(terminal_growth=0.5, required_return=0.1),   # terminal not below required
    dict(terminal_growth=0.1, required_return=0.1),   # equal is not below either
    dict(required_return=1.5),
])
def test_scenario_bounds(bad):
    with pytest.raises(ValidationError):
        Scenario(**{"name": "s", "fcf_growth": 0.05, "years": 5, **bad})


def test_scenario_growth_may_reach_the_bracket_end():
    assert GROWTH_HIGH == 1.0
    assert Scenario(name="s", fcf_growth=GROWTH_HIGH, years=50).fcf_growth == 1.0


def test_malformed_file_is_a_typed_error_naming_the_path(tmp_path):
    path = observation_path(tmp_path, "KO")
    path.parent.mkdir()
    for raw in (b"{not json", json.dumps({"ticker": "KO"}).encode(), b"\xff\xfe\x00",
                json.dumps([1, 2]).encode()):
        path.write_bytes(raw)
        with pytest.raises(ObservationError) as e:
            read_observation(path)
        assert str(path) in str(e.value)
        # Present but invalid never reads as absent.
        with pytest.raises(ObservationError):
            find_observation(tmp_path, "KO")


def test_a_file_naming_another_ticker_is_refused(tmp_path):
    write_observation(tmp_path, _obs(ticker="KO"))
    path = observation_path(tmp_path, "KO")
    path.rename(observation_path(tmp_path, "PEP"))
    with pytest.raises(ObservationError, match="PEP"):
        find_observation(tmp_path, "PEP")


def test_a_symlink_at_the_path_is_refused_and_never_followed(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text(_obs().model_dump_json())
    before = victim.read_bytes()
    path = observation_path(tmp_path, "KO")
    path.parent.mkdir()
    path.symlink_to(victim)
    with pytest.raises(ObservationError, match="symlink"):
        read_observation(path)
    with pytest.raises(ObservationError, match="symlink"):
        find_observation(tmp_path, "KO")
    with pytest.raises(OSError):
        write_observation(tmp_path, _obs(price=1.0))
    with pytest.raises(OSError):
        remove_observation(tmp_path, "KO")
    assert path.is_symlink() and victim.read_bytes() == before


def test_a_symlinked_market_directory_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "market").symlink_to(outside)
    with pytest.raises(OSError):
        write_observation(tmp_path, _obs())
    assert list(outside.iterdir()) == []


def test_remove(tmp_path):
    path = write_observation(tmp_path, _obs())
    assert remove_observation(tmp_path, "KO") == path
    assert not path.exists()
    assert remove_observation(tmp_path, "KO") is None


def test_age_and_staleness():
    obs = _obs(observed_at=datetime(2026, 9, 25, 20, 0, tzinfo=UTC))
    assert STALE_AFTER_DAYS == 7
    assert obs.age_days(date(2026, 10, 2)) == 7 and not obs.is_stale(date(2026, 10, 2))
    assert obs.age_days(date(2026, 10, 3)) == 8 and obs.is_stale(date(2026, 10, 3))
    # The day is the observation's US/Eastern calendar day — EDGAR's
    # (review of 48b1f04, F5) — not the UTC one.
    assert EASTERN == ZoneInfo("America/New_York")
    late = _obs(observed_at=datetime(2026, 10, 3, 3, 30, tzinfo=UTC))  # 23:30 ET on the 2nd
    assert late.eastern_day == date(2026, 10, 2)
    assert late.age_days(date(2026, 10, 3)) == 1
    assert _obs(observed_at=datetime(2026, 10, 2, 23, 59, tzinfo=UTC)).age_days(date(2026, 10, 3)) == 1


def test_age_is_never_negative():
    # 22:30 Eastern on the report's own day: age 0, not -1 (F5); a report
    # dated before the observation's day (a clock skew) still reads 0.
    same_day = _obs(observed_at=datetime(2026, 10, 3, 22, 30, tzinfo=EASTERN),
                    recorded_at=datetime(2026, 10, 4, 3, 0, tzinfo=UTC))
    assert same_day.age_days(date(2026, 10, 3)) == 0 and not same_day.is_stale(date(2026, 10, 3))
    assert same_day.age_days(date(2026, 10, 1)) == 0


def test_the_build_day_is_the_eastern_one():
    """Review of f73b059, R2: the age is counted on EDGAR's day at build
    time — 01:05 UTC on the 3rd is still the evening of the 2nd in New
    York — never on the host-local date."""
    assert eastern_today(datetime(2026, 10, 3, 1, 5, tzinfo=UTC)) == date(2026, 10, 2)
    assert eastern_today(datetime(2026, 10, 3, 4, 5, tzinfo=UTC)) == date(2026, 10, 3)
    assert eastern_today(datetime(2026, 10, 3, 0, 5, tzinfo=EASTERN)) == date(2026, 10, 3)
    assert eastern_today() == datetime.now(EASTERN).date()
    with pytest.raises(ValueError, match="offset"):
        eastern_today(datetime(2026, 10, 3, 1, 5))


# --- the future is refused on the way in (F2) --------------------------------------


def test_a_future_observation_is_refused_on_read_naming_the_path(tmp_path):
    # observed_at ≤ recorded_at is consistent, so the model accepts a file
    # dated 2999 throughout; the reader does not (review of 48b1f04, F2).
    future = _obs(observed_at=datetime(2999, 1, 1, tzinfo=UTC),
                  recorded_at=datetime(2999, 1, 2, tzinfo=UTC))
    path = write_observation(tmp_path, future)
    for reader in (lambda: read_observation(path), lambda: load_observation(path),
                   lambda: find_observation(tmp_path, "KO")):
        with pytest.raises(ObservationError, match="future") as e:
            reader()
        assert str(path) in str(e.value)
    # With the clock set past it, the same file reads.
    assert read_observation(path, now=datetime(2999, 1, 3, tzinfo=UTC)) == future
    assert find_observation(tmp_path, "KO", now=datetime(2999, 1, 3, tzinfo=UTC)).observation == future


def test_a_future_recorded_at_alone_is_refused_too(tmp_path):
    obs = _obs(observed_at=NOW - timedelta(hours=1), recorded_at=NOW + timedelta(days=1))
    path = write_observation(tmp_path, obs)
    with pytest.raises(ObservationError, match="recorded_at"):
        read_observation(path, now=NOW)
    assert read_observation(path, now=NOW + timedelta(days=1)) == obs


def test_a_typed_time_is_iso_8601_with_its_offset_and_nothing_else():
    """`parse_observed_at`: the one parser of a typed time, for `market.py
    record --at` and the workbench's price form (independent review of
    9d00328: the form let the model read a bare number as Unix seconds)."""
    import re

    from app.services.valuation.observation import parse_observed_at

    assert parse_observed_at("2026-10-02T17:00:00-04:00") == datetime(2026, 10, 2, 21, tzinfo=UTC)
    assert parse_observed_at("2026-10-02T21:00Z") == datetime(2026, 10, 2, 21, tzinfo=UTC)
    for bad in ("0", "1700000000", "61.20", "-1", "yesterday", ""):
        with pytest.raises(ValueError, match=f"^observed_at {re.escape(repr(bad))}: "):
            parse_observed_at(bad)
    for naive in ("2026-10-02T17:00", "2026-10-02"):
        with pytest.raises(ValueError, match="has no UTC offset"):
            parse_observed_at(naive)
    # The CLI names its flag, as it always has.
    with pytest.raises(ValueError, match=r"^--at '0': "):
        parse_observed_at("0", name="--at")


def test_a_recorded_observation_is_durable_with_its_folder(tmp_path, monkeypatch):
    """The folder is fsynced after the rename (Hermes re-audit of #118 @
    34836cf): a rename alone can be lost on power loss."""
    import os

    seen: list[str] = []
    real = os.fsync

    def fsync(fd):
        seen.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    path = write_observation(tmp_path, _obs())
    assert seen[-1] == str(path.parent)
