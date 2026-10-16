"""Valuation result contract with market and filing provenance."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.schemas.metrics import MetricResult
from app.services.market_data import MarketSnapshot


class ValuationAnalysis(BaseModel):
    ticker: str
    currency: str
    market_snapshot: MarketSnapshot
    fundamentals_source: str
    financial_period_end: date
    period_basis: str
    shares_basis: str
    metrics: dict[str, MetricResult]
