"""The whole plane for one report run: the observation, the dataset as
filed by it, the bridge over that dataset, the TTM figures, the multiples
and the expectations, computed once and handed to the renderer (`render`)
and the ledger (`reporting.ledger`). Pure over its inputs: the datasets are
read, never written, and the analysis result is not an input at all — the
plane cannot see a score.

The facts are read as filed by the observation (review of 48b1f04, F1).
The report's dataset is latest-filed-wins over the whole payload, so an
FY-end quarter carries the date of the 10-Q that later repeated it, and a
restated figure is the restated one; neither is what an observation before
that later filing could have seen. With the raw companyfacts payload in
hand the plane maps its own dataset through the mapper's point-in-time cut
— `pit.build_pit_dataset`, the backtests' boundary, imported and not
changed — as of `bridge.available_through(observed_at)`. Without the raw
payload (a dataset-only caller) it uses the report's dataset and the card
says the check was not made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.schemas.financials import CompanyDataset
from app.services.backtesting.pit import build_pit_dataset
from app.services.valuation.bridge import (
    Bridge,
    available_through,
    enterprise_value_bridge,
)
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
    # The dataset the bridge and the TTM figures were read from: the
    # as-filed one when the raw facts were in hand, else the report's own;
    # None when nothing could be mapped as of the observation.
    dataset: CompanyDataset | None
    # The point-in-time cut that dataset was mapped through; None when the
    # check was not made.
    as_filed_by: date | None
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


def as_filed_dataset(
    dataset: CompanyDataset, company_facts: dict[str, Any], obs: MarketObservation
) -> tuple[CompanyDataset | None, date]:
    """The dataset a reader on the observation's day could have mapped from
    the raw payload: the same window as the report's (`len(periods)`), the
    same profile, only facts filed through `available_through`. None when
    fewer than two quarter ends were filed by then (the mapper refuses)."""
    through = available_through(obs.observed_at)
    try:
        pit, _ = build_pit_dataset(
            company_facts, dataset.profile.ticker, through, n_quarters=len(dataset.periods),
            sector=dataset.profile.sector,
        )
    except ValueError:
        return None, through
    return pit, through


def compute_plane(dataset: CompanyDataset, loaded: LoadedObservation, generated_on: date, *,
                  company_facts: dict[str, Any] | None = None) -> ValuationPlane:
    """The plane over `dataset` for `loaded`'s observation, as of the
    report's day (which dates the observation's age); over the facts as
    filed by the observation when `company_facts` (the raw payload the
    dataset was mapped from) is given. An observation for another ticker
    is refused: it is the one market datum in the run, and a price of the
    wrong company under this card is the worst line it could carry."""
    obs = loaded.observation
    ticker = dataset.profile.ticker.upper()
    if obs.ticker != ticker:
        raise ValueError(f"market observation is for {obs.ticker}, not {ticker}")
    read: CompanyDataset | None = dataset
    through: date | None = None
    if company_facts is not None:
        read, through = as_filed_dataset(dataset, company_facts, obs)
    bridge = enterprise_value_bridge(read, obs, as_filed_by=through)
    ttm = trailing(read, bridge)
    return ValuationPlane(
        loaded, generated_on, read, through, bridge, ttm, compute_multiples(bridge, ttm),
        compute_expectations(bridge, ttm, obs),
    )
