"""Deterministic trailing valuation from market snapshots and SEC fundamentals."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from app.schemas.financials import CompanyDataset, PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.valuation import ValuationAnalysis
from app.services.formulas.ttm import annualize
from app.services.market_data import MarketSnapshot

_FUNDAMENTALS_SOURCE = "SEC filings (normalized Companyfacts)"
_PERIOD_BASIS = "TTM: four latest reported quarters"
_SHARES_BASIS = "Latest reported point-in-time shares outstanding"


def _metric(
    name: str,
    formula: str,
    fiscal_label: str,
    inputs: Mapping[str, float | None],
    *,
    value: float | None = None,
    missing_fields: list[str] | None = None,
    not_meaningful: str | None = None,
) -> MetricResult:
    if missing_fields:
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.MISSING_DATA,
            inputs=dict(inputs),
            missing_fields=missing_fields,
        )
    if not_meaningful is not None:
        return MetricResult(
            name=name,
            formula=formula,
            fiscal_label=fiscal_label,
            status=MetricStatus.NOT_MEANINGFUL,
            inputs=dict(inputs),
            note=not_meaningful,
        )
    if value is None or not isfinite(value):
        raise ValueError(f"non-finite valuation metric: {name}")
    return MetricResult(
        name=name,
        formula=formula,
        fiscal_label=fiscal_label,
        value=value,
        status=MetricStatus.OK,
        inputs=dict(inputs),
    )


def _missing(inputs: Mapping[str, float | None]) -> list[str]:
    return [name for name, value in inputs.items() if value is None]


def _ratio(
    name: str,
    formula: str,
    fiscal_label: str,
    numerator_name: str,
    numerator: float | None,
    denominator_name: str,
    denominator: float | None,
    *,
    require_positive_denominator: bool = True,
) -> MetricResult:
    inputs = {numerator_name: numerator, denominator_name: denominator}
    missing = _missing(inputs)
    if missing:
        return _metric(name, formula, fiscal_label, inputs, missing_fields=missing)
    assert numerator is not None and denominator is not None
    if denominator == 0 or (require_positive_denominator and denominator < 0):
        return _metric(
            name,
            formula,
            fiscal_label,
            inputs,
            not_meaningful=f"{denominator_name} must be positive",
        )
    return _metric(
        name,
        formula,
        fiscal_label,
        inputs,
        value=numerator / denominator,
    )


def _latest_ttm(dataset: CompanyDataset) -> tuple[PeriodFinancials, PeriodFinancials]:
    periods = dataset.sorted_periods()
    if not periods:
        raise ValueError("valuation requires reported financial periods")
    latest = periods[-1]
    ttm = annualize(periods, len(periods) - 1)
    if ttm is None:
        raise ValueError("valuation requires four consecutive quarterly periods")
    return latest, ttm


def compute_trailing_valuation(
    dataset: CompanyDataset,
    market_snapshot: MarketSnapshot,
) -> ValuationAnalysis:
    """Calculate trailing valuation without provider-computed ratio inputs."""
    fundamentals_ticker = dataset.profile.ticker.strip().upper()
    market_ticker = market_snapshot.ticker.strip().upper()
    if fundamentals_ticker != market_ticker:
        raise ValueError(
            f"ticker mismatch: fundamentals={fundamentals_ticker}, market={market_ticker}"
        )
    filing_currency = dataset.profile.reporting_currency
    if filing_currency is None or not filing_currency.strip():
        raise ValueError("filing currency unavailable; cross-currency valuation is disabled")
    normalized_filing_currency = filing_currency.strip().upper()
    normalized_market_currency = market_snapshot.currency.strip().upper()
    if normalized_filing_currency != normalized_market_currency:
        raise ValueError(
            "currency mismatch: "
            f"fundamentals={normalized_filing_currency}, market={normalized_market_currency}"
        )
    latest, ttm = _latest_ttm(dataset)
    fiscal_label = ttm.fiscal_label
    shares = latest.shares_outstanding
    price = market_snapshot.price

    market_cap: float | None = None
    market_cap_inputs = {"market_price": price, "shares_outstanding": shares}
    market_cap_missing = _missing(market_cap_inputs)
    if market_cap_missing:
        market_cap_metric = _metric(
            "market_cap",
            "Market price × latest reported shares outstanding",
            fiscal_label,
            market_cap_inputs,
            missing_fields=market_cap_missing,
        )
    elif shares is not None and shares <= 0:
        market_cap_metric = _metric(
            "market_cap",
            "Market price × latest reported shares outstanding",
            fiscal_label,
            market_cap_inputs,
            not_meaningful="shares_outstanding must be positive",
        )
    else:
        assert shares is not None
        market_cap = price * shares
        market_cap_metric = _metric(
            "market_cap",
            "Market price × latest reported shares outstanding",
            fiscal_label,
            market_cap_inputs,
            value=market_cap,
        )

    net_debt: float | None = None
    if latest.total_debt is not None and latest.cash_and_equivalents is not None:
        net_debt = latest.total_debt - latest.cash_and_equivalents
    enterprise_value: float | None = None
    ev_inputs = {
        "market_cap": market_cap,
        "total_debt": latest.total_debt,
        "cash_and_equivalents": latest.cash_and_equivalents,
    }
    ev_missing = _missing(ev_inputs)
    if ev_missing:
        ev_metric = _metric(
            "enterprise_value",
            "Market cap + total debt − cash and equivalents",
            fiscal_label,
            ev_inputs,
            missing_fields=ev_missing,
        )
    else:
        assert market_cap is not None and net_debt is not None
        enterprise_value = market_cap + net_debt
        ev_metric = _metric(
            "enterprise_value",
            "Market cap + total debt − cash and equivalents",
            fiscal_label,
            ev_inputs,
            value=enterprise_value,
        )

    metrics = {
        "market_cap": market_cap_metric,
        "trailing_pe": _ratio(
            "trailing_pe", "Market cap ÷ TTM net income", fiscal_label,
            "market_cap", market_cap, "ttm_net_income", ttm.net_income,
        ),
        "price_to_sales": _ratio(
            "price_to_sales", "Market cap ÷ TTM revenue", fiscal_label,
            "market_cap", market_cap, "ttm_revenue", ttm.revenue,
        ),
        "enterprise_value": ev_metric,
        "ev_to_ebitda": _ratio(
            "ev_to_ebitda", "Enterprise value ÷ TTM EBITDA", fiscal_label,
            "enterprise_value", enterprise_value, "ttm_ebitda", ttm.ebitda,
        ),
        "price_to_fcf": _ratio(
            "price_to_fcf", "Market cap ÷ TTM free cash flow", fiscal_label,
            "market_cap", market_cap, "ttm_fcf", ttm.fcf,
        ),
        "fcf_yield": _ratio(
            "fcf_yield", "TTM free cash flow ÷ market cap", fiscal_label,
            "ttm_fcf", ttm.fcf, "market_cap", market_cap,
        ),
        "earnings_yield": _ratio(
            "earnings_yield", "TTM net income ÷ market cap", fiscal_label,
            "ttm_net_income", ttm.net_income, "market_cap", market_cap,
            require_positive_denominator=True,
        ),
        "net_debt_to_ebitda": _ratio(
            "net_debt_to_ebitda", "Net debt ÷ TTM EBITDA", fiscal_label,
            "net_debt", net_debt, "ttm_ebitda", ttm.ebitda,
        ),
    }
    if ttm.net_income is not None and ttm.net_income <= 0:
        metrics["earnings_yield"] = _metric(
            "earnings_yield",
            "TTM net income ÷ market cap",
            fiscal_label,
            {"ttm_net_income": ttm.net_income, "market_cap": market_cap},
            not_meaningful="TTM net income must be positive",
        )

    return ValuationAnalysis(
        ticker=dataset.profile.ticker.upper(),
        currency=market_snapshot.currency,
        market_snapshot=market_snapshot,
        fundamentals_source=_FUNDAMENTALS_SOURCE,
        financial_period_end=latest.period_end,
        period_basis=_PERIOD_BASIS,
        shares_basis=_SHARES_BASIS,
        metrics=metrics,
    )
