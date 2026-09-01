"""Walk-forward backtest runner.

For each company and each as-of date: build the point-in-time dataset (facts
filed <= as-of only), run the UNMODIFIED analysis pipeline, and record scores
alongside forward outcomes. Signals never see the future; outcomes do.

Output: one CSV row per (ticker, as-of). Skips are recorded with a reason
rather than silently dropped.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, timedelta
from pathlib import Path

from app.config import scoring_config as cfg
from app.core.pipeline import analyze
from app.services.backtesting.events import fetch_entity_events
from app.services.backtesting.outcomes import forward_outcomes
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.prices import PriceClient, relative_forward_returns
from app.services.backtesting.universe import UNIVERSE, UniverseMember
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.sec_client import SecClient

logger = logging.getLogger(__name__)

FILING_LAG_DAYS = 75
STALENESS_LIMIT_DAYS = 150
MIN_PIT_PERIODS = 6
PRICE_START = date(2019, 1, 1)
RESTATEMENT_WINDOW_DAYS = 730

DEFAULT_ASOF_QUARTER_ENDS = [
    date(y, m, d)
    for y in range(2021, 2026)
    for (m, d) in ((3, 31), (6, 30), (9, 30), (12, 31))
    if date(y, m, d) <= date(2025, 3, 31)
]


def component_metric_names() -> list[str]:
    names: list[str] = []
    for block in cfg.BLOCKS:
        for ms in block.metrics:
            if ms.metric_name not in names:
                names.append(ms.metric_name)
    return names


def csv_header() -> list[str]:
    return (
        ["ticker", "archetype", "sector", "asof", "status", "latest_period", "n_periods", "overall"]
        + [f"blk_{b.name.replace(' ', '_')}" for b in cfg.BLOCKS]
        + [f"c_{n}" for n in component_metric_names()]
        + ["rel_3m", "rel_6m", "rel_12m", "op_margin_chg_4q", "fcf_margin_chg_4q",
           "ni_growth_fwd_4q", "non_reliance_24m"]
    )


def _score_columns(result) -> dict[str, float | str]:
    row: dict[str, float | str] = {}
    row["overall"] = result.overall.score if result.overall and result.overall.score is not None else ""
    concerns: dict[str, float] = {}
    for bs in result.block_scores:
        row[f"blk_{bs.name.replace(' ', '_')}"] = bs.score if bs.score is not None else ""
        for comp in bs.components:
            if comp.concern_score is not None and comp.metric_name not in concerns:
                concerns[comp.metric_name] = comp.concern_score
    for name in component_metric_names():
        row[f"c_{name}"] = concerns.get(name, "")
    return row


def run_backtest(
    out_path: str | Path,
    members: list[UniverseMember] | None = None,
    asof_quarter_ends: list[date] | None = None,
    n_quarters: int = 8,
) -> Path:
    members = members or UNIVERSE
    asof_qs = asof_quarter_ends or DEFAULT_ASOF_QUARTER_ENDS
    sec = SecClient()
    prices = PriceClient()
    today = date.today()

    spy = prices.fetch("SPY", PRICE_START, today)
    if spy is None:
        raise RuntimeError("Benchmark (SPY) prices unavailable; cannot compute relative returns.")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = csv_header()

    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()

        for member in members:
            try:
                facts = sec.company_facts(member.ticker)
            except Exception as e:  # noqa: BLE001
                logger.warning("companyfacts failed for %s: %s", member.ticker, e)
                continue
            trimmed = trim_to_mapped_tags(facts)
            events = fetch_entity_events(sec, member.ticker)
            series = prices.fetch(member.ticker, PRICE_START, today)

            try:
                full_ds, _ = build_dataset(facts, member.ticker, n_quarters=28, sector=member.sector)
            except ValueError as e:
                logger.warning("full dataset failed for %s: %s", member.ticker, e)
                continue

            for qend in asof_qs:
                asof = qend + timedelta(days=FILING_LAG_DAYS)
                base: dict[str, object] = {
                    "ticker": member.ticker,
                    "archetype": member.archetype,
                    "sector": member.sector,
                    "asof": asof.isoformat(),
                }
                try:
                    pit_ds, _ = build_pit_dataset(
                        trimmed, member.ticker, asof, n_quarters=n_quarters, sector=member.sector
                    )
                except ValueError:
                    writer.writerow({**base, "status": "skip_no_pit_data"})
                    continue

                pit_ds.profile.is_financial_institution = events.is_financial_institution
                latest = pit_ds.periods[-1].period_end
                if (asof - latest).days > STALENESS_LIMIT_DAYS:
                    writer.writerow({**base, "status": "skip_stale", "latest_period": latest.isoformat()})
                    continue
                if len(pit_ds.periods) < MIN_PIT_PERIODS:
                    writer.writerow(
                        {**base, "status": "skip_short_history", "n_periods": len(pit_ds.periods)}
                    )
                    continue

                result = analyze(pit_ds)
                if result.excluded:
                    writer.writerow({**base, "status": "excluded_financial"})
                    continue

                row: dict[str, object] = {
                    **base,
                    "status": "ok",
                    "latest_period": latest.isoformat(),
                    "n_periods": len(pit_ds.periods),
                    **_score_columns(result),
                }
                if series is not None:
                    row.update(
                        {k: (v if v is not None else "") for k, v in
                         relative_forward_returns(series, spy, asof).items()}
                    )
                row.update(
                    {
                        k: (v if v is not None else "")
                        # anchor=latest: outcomes must start from the quarter the
                        # signal was scored on (last FILED), not the last ended
                        for k, v in forward_outcomes(full_ds, asof, anchor=latest).items()
                    }
                )
                row["non_reliance_24m"] = int(events.non_reliance_within(asof, RESTATEMENT_WINDOW_DAYS))
                writer.writerow(row)
            logger.info("done %s", member.ticker)
    return out_path
