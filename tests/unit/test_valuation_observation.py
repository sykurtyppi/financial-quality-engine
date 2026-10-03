"""The market observation: the operator's one price per ticker, recorded with
its exact timestamp and what was looked at (Hermes review of 02c2aac,
valuation plane). It is the only market datum the engine holds, so it is
validated hard on the way in and never read through a symlink."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.valuation.observation import (
    STALE_AFTER_DAYS,
    Assumptions,
    LoadedObservation,
    MarketObservation,
    ObservationError,
    Scenario,
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
    assert find_observation(tmp_path, "KO") == loaded
    assert find_observation(tmp_path, "AAPL") is None
    # Only the observation itself is on disk: no temporary beside it.
    assert sorted(p.name for p in path.parent.iterdir()) == ["KO.json"]


def test_loaded_of_an_in_memory_observation_hashes_its_canonical_json():
    obs = _obs()
    loaded = LoadedObservation.of(obs)
    assert loaded.path is None and loaded.observation == obs
    assert loaded.sha256 == hashlib.sha256(obs.model_dump_json().encode()).hexdigest()


@pytest.mark.parametrize("bad", [
    dict(observed_at=datetime(2026, 10, 3, 14)),              # naive
    dict(recorded_at=datetime(2026, 10, 3, 15)),              # naive
    dict(observed_at=NOW + timedelta(seconds=1)),             # after recorded_at
    dict(price=0.0), dict(price=-1.0), dict(price=float("nan")), dict(price=float("inf")),
    dict(currency="usd"), dict(currency="US"), dict(currency="USDX"), dict(currency="€UR"),
    dict(ticker="../KO"), dict(ticker=""), dict(ticker="K O"),
    dict(source=""), dict(source="   "), dict(source="x" * 201),
    dict(note="n" * 501),
])
def test_invalid_observations_are_refused(bad):
    with pytest.raises(ValidationError):
        _obs(**bad)


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
    dict(terminal_growth=0.5, required_return=0.1),   # terminal not below required
    dict(terminal_growth=0.1, required_return=0.1),   # equal is not below either
    dict(required_return=1.5),
])
def test_scenario_bounds(bad):
    with pytest.raises(ValidationError):
        Scenario(**{"name": "s", "fcf_growth": 0.05, "years": 5, **bad})


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
    # The day is the observation's own (UTC), not a local one.
    assert _obs(observed_at=datetime(2026, 10, 2, 23, 59, tzinfo=UTC)).age_days(date(2026, 10, 3)) == 1
