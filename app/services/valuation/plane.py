"""The whole plane for one report run: the observation, the bridge over the
dataset, the TTM figures, the multiples and the expectations, computed once
and handed to the renderer (`render`) and the ledger (`reporting.ledger`).
Pure over its inputs: the dataset is read, never written, and the analysis
result is not an input at all — the plane cannot see a score."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.schemas.financials import CompanyDataset
from app.services.valuation.bridge import Bridge, enterprise_value_bridge
from app.services.valuation.expectations import Expectations, compute_expectations
from app.services.valuation.multiples import (
    Multiple,
    TrailingFigures,
    compute_multiples,
    trailing,
)
from app.services.valuation.observation import LoadedObservation, MarketObservation


@dataclass(frozen=True)
class ValuationPlane:
    loaded: LoadedObservation
    generated_on: date
    bridge: Bridge
    ttm: TrailingFigures
    multiples: tuple[Multiple, ...]
    expectations: Expectations

    @property
    def observation(self) -> MarketObservation:
        return self.loaded.observation

    @property
    def age_days(self) -> int:
        return self.observation.age_days(self.generated_on)

    @property
    def stale(self) -> bool:
        return self.observation.is_stale(self.generated_on)


def compute_plane(dataset: CompanyDataset, loaded: LoadedObservation,
                  generated_on: date) -> ValuationPlane:
    """The plane over `dataset` for `loaded`'s observation, as of the
    report's day (which dates the observation's age). An observation for
    another ticker is refused: it is the one market datum in the run, and a
    price of the wrong company under this card is the worst line it could
    carry."""
    obs = loaded.observation
    ticker = dataset.profile.ticker.upper()
    if obs.ticker != ticker:
        raise ValueError(f"market observation is for {obs.ticker}, not {ticker}")
    bridge = enterprise_value_bridge(dataset, obs)
    ttm = trailing(dataset, bridge)
    return ValuationPlane(
        loaded, generated_on, bridge, ttm, compute_multiples(bridge, ttm),
        compute_expectations(bridge, ttm, obs),
    )
