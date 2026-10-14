"""Every backtest row says what the mapper built it from.

The backtests called `build_pit_dataset` and threw its diagnostics away, so
a score that moved between two runs could not be told apart from a field
that moved to another XBRL concept without re-running the mapper. Rows now
carry field coverage and a fingerprint of the selections
(`IngestionDiagnostics.selections_digest`). The runner's CSV gains two
columns at the end; every earlier column keeps its position and value.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from app.config import scoring_config as cfg
from app.services.backtesting import runner
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.universe import UniverseMember
from app.services.ingestion.companyfacts_mapper import build_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"


def _facts(ticker: str = "AAPL") -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


def test_the_header_is_the_old_header_plus_two_columns_at_the_end():
    legacy = (
        ["ticker", "archetype", "sector", "asof", "status", "latest_period", "n_periods", "overall"]
        + [f"blk_{b.name.replace(' ', '_')}" for b in cfg.BLOCKS]
        + [f"c_{n}" for n in runner.component_metric_names()]
        + ["rel_3m", "rel_6m", "rel_12m", "op_margin_chg_4q", "fcf_margin_chg_4q",
           "ni_growth_fwd_4q", "non_reliance_24m"]
    )
    assert runner.csv_header() == legacy + ["coverage", "selections_digest"]


def test_the_digest_names_the_selections_and_nothing_else():
    _ds, diag = build_dataset(_facts(), "AAPL")
    again = build_dataset(_facts(), "AAPL")[1]
    assert diag.selections_digest() == again.selections_digest()
    assert len(diag.selections_digest()) == 12
    # Another ticker label, same concepts: same digest.
    assert build_dataset(_facts(), "X")[1].selections_digest() == diag.selections_digest()
    # One field moved to another concept: a different digest.
    moved = diag.model_copy(deep=True)
    revenue = moved.field_by_name("revenue")
    object.__setattr__(revenue, "tag_used", "us-gaap:SalesRevenueNet")
    assert moved.selections_digest() != diag.selections_digest()
    # The same selections listed in another order (a registry reordering):
    # the same digest — it fingerprints the choices, not their listing.
    reordered = diag.model_copy(update={"fields": list(reversed(diag.fields))})
    assert reordered.selections_digest() == diag.selections_digest()
    # Different companies map differently.
    assert build_dataset(_facts("KO"), "KO")[1].selections_digest() != diag.selections_digest()


class _Sec:
    def __init__(self, facts):
        self._facts = facts


class _Prices:
    def fetch(self, ticker, start, end):
        return object()  # a series stand-in; returns are stubbed below


def test_a_runner_row_carries_the_coverage_and_digest_of_its_own_pit_dataset(monkeypatch, tmp_path):
    facts = _facts()
    monkeypatch.setattr(runner, "SecClient", lambda: _Sec(facts))
    monkeypatch.setattr(runner, "PriceClient", _Prices)
    monkeypatch.setattr(runner, "fetch_member_facts", lambda sec, member: facts)
    from app.services.backtesting.events import EntityEvents

    monkeypatch.setattr(runner, "fetch_entity_events",
                        lambda *a, **k: EntityEvents("AAPL", 3571, "x", []))
    monkeypatch.setattr(runner, "relative_forward_returns", lambda *a, **k: {})
    member = UniverseMember("AAPL", "control_tech", "Technology")
    cuts = [date(2024, 9, 30), date(2025, 6, 30), date(2005, 3, 31)]
    out = runner.run_backtest(tmp_path / "bt.csv", members=[member], asof_quarter_ends=cuts)

    rows = list(csv.DictReader(out.open()))
    assert list(rows[0]) == runner.csv_header()
    by_asof = {r["asof"]: r for r in rows}
    trimmed = trim_to_mapped_tags(facts)
    checked = 0
    for qend in cuts:
        asof = qend.fromordinal(qend.toordinal() + runner.FILING_LAG_DAYS)
        row = by_asof[asof.isoformat()]
        if row["status"] == "skip_no_pit_data":
            assert row["coverage"] == row["selections_digest"] == ""
            continue
        _ds, diag = build_pit_dataset(trimmed, "AAPL", asof)
        assert float(row["coverage"]) == round(diag.coverage(), 3)
        assert row["selections_digest"] == diag.selections_digest()
        checked += 1
    assert checked == 2
    assert by_asof["2005-06-14"]["status"] == "skip_no_pit_data"


def test_every_control_result_carries_the_same_two_fields():
    from dataclasses import fields

    from app.services.backtesting import (
        clean_narrative_control,
        narrative_timing,
        restatement_control,
        restatement_narrative,
        survivorship,
    )

    for cls in (survivorship.HorizonResult, restatement_control.HorizonScore,
                restatement_narrative.NarrativeResult, narrative_timing.TimingResult,
                clean_narrative_control.CleanResult):
        names = {f.name for f in fields(cls)}
        assert {"coverage", "selections_digest"} <= names, cls.__name__


def test_a_survivorship_horizon_records_what_the_mapper_used(monkeypatch):
    from app.services.backtesting import survivorship

    facts = _facts()

    class Client:
        def submissions_by_cik(self, cik):
            return {"sic": "3571"}

        def company_facts_by_cik(self, cik):
            return facts

    company = survivorship.DeadCompany("Apple test", 320193, date(2026, 3, 1), "delisting")
    result = survivorship.evaluate_company(Client(), company)
    scored = [h for h in result.horizons if h.status in ("ok", "no_score")]
    assert scored
    trimmed = trim_to_mapped_tags(facts)
    for h in scored:
        _ds, diag = build_pit_dataset(trimmed, "Apple test", h.asof)
        assert h.selections_digest == diag.selections_digest()
        assert h.coverage == round(diag.coverage(), 2)


def test_a_clean_control_result_records_what_the_mapper_used(monkeypatch):
    from types import SimpleNamespace

    from app.services.backtesting import clean_narrative_control as ccn

    facts = _facts()

    class Client:
        def resolve_cik(self, ticker):
            return 320193

        def company_facts(self, ticker):
            return facts

    monkeypatch.setattr(ccn, "first_402_date", lambda client, cik: None)
    monkeypatch.setattr(ccn, "fetch_documents",
                        lambda *a, **k: SimpleNamespace(documents=[], diagnostics=[]))
    company = ccn.CleanCompany("Apple test", "AAPL", "Technology", "")
    result = ccn.evaluate_clean(Client(), company, anchor=date(2025, 6, 30))
    _ds, diag = build_pit_dataset(trim_to_mapped_tags(facts), "Apple test", date(2025, 6, 30))
    assert result.error is None
    assert result.selections_digest == diag.selections_digest()
    assert result.coverage == round(diag.coverage(), 2)
