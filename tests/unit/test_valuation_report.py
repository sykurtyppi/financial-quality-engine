"""The valuation shadow card never touches the score (Hermes review of
02c2aac, valuation plane): with and without a market observation the
decision card, the thermometer, the result and every non-valuation ledger
item are byte-identical, and only the appendix section and the valuation
rows differ. An observation file that is present but invalid fails the
build closed, like a ledger that cannot be built."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.schemas.ledger import LedgerDocument, Plane
from app.services.ingestion import sec_client
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.journal import reporting as journal_reporting
from app.services.reporting.report_builder import build_report, ledger_path
from app.services.reporting.report_files import NotPublished
from app.services.valuation.observation import (
    LoadedObservation,
    MarketObservation,
    observation_path,
    write_observation,
)
from app.services.valuation.render import SECTION_TITLE, not_produced_line
from scripts import drill

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
CIKS = {"AAPL": 320193, "KO": 21344, "CRM": 1108524}
TITLES = {"AAPL": "Apple Inc.", "KO": "COCA COLA CO", "CRM": "Salesforce, Inc."}
DAY = "2026-10-03"
OBSERVED = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
RECORDED = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
FETCHED = "2026-10-03 09:00 UTC"


def _facts(ticker: str) -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


def _obs(ticker: str = "KO", observed_at: datetime = OBSERVED,
         recorded_at: datetime = RECORDED, **kw) -> MarketObservation:
    return MarketObservation(ticker=ticker, price=66.25, currency="USD", observed_at=observed_at,
                             source="NYSE official close (broker statement)",
                             recorded_at=recorded_at, **kw)


def _obs_now(ticker: str = "KO") -> MarketObservation:
    """An observation of two hours ago, recorded now (the live path)."""
    now = datetime.now(UTC)
    return _obs(ticker, observed_at=now - timedelta(hours=2), recorded_at=now)


class _Client:
    """The SEC client as the streams see it, answering from the fixture and
    a synthetic filing index; nothing else is reachable."""

    def __init__(self, ticker: str, facts: dict):
        self._facts = facts
        self._index = drill._index(CIKS[ticker], ticker, date.fromisoformat(DAY))

    def resolve_cik(self, ticker):
        return CIKS[ticker]

    def company_facts(self, ticker):
        return self._facts

    def company_facts_by_cik(self, cik):
        return self._facts

    def submissions(self, ticker):
        return self._index

    def submissions_by_cik(self, cik):
        return self._index

    def archive_text(self, *a, **k):
        raise sec_client.SecClientError("test: no network")

    def _get(self, url):
        raise sec_client.SecClientError(f"test: no network ({url})")


def _build(ticker: str, tmp_path: Path, *, with_client: bool, observation=None,
           requested: bool = False, generated_on: str = DAY):
    facts = _facts(ticker)
    ds, diag = build_dataset(facts, ticker)
    result = analyze(ds)
    before = result.model_dump_json()
    out = tmp_path / f"{ticker}_{'with' if observation else 'without'}_{requested}.ledger.json"
    kw = dict(client=_Client(ticker, facts), company_facts=facts,
              submissions=_Client(ticker, facts).submissions(ticker)) if with_client else {}
    report, thermometer = build_report(
        result, ds, generated_on=generated_on, coverage=diag.coverage(),
        field_tags=diag.selected_series(), ticker=ticker, fetched_at=FETCHED,
        field_notes=diag.field_notes(), ledger_out=out,
        market_observation=observation, valuation_requested=requested, **kw,
    )
    assert result.model_dump_json() == before  # the plane never writes into the result
    return report, thermometer, LedgerDocument.model_validate_json(out.read_text())


def _card(report: str) -> str:
    return report.split("# Full report (appendix)")[0]


def _non_valuation(doc: LedgerDocument) -> list:
    return [i for i in doc.items if i.plane is not Plane.VALUATION]


@pytest.mark.parametrize("ticker", sorted(CIKS))
@pytest.mark.parametrize("with_client", [False, True], ids=["dataset-only", "with-streams"])
def test_card_scores_and_every_other_ledger_item_are_byte_identical(ticker, tmp_path, with_client):
    plain, therm0, doc0 = _build(ticker, tmp_path, with_client=with_client)
    loaded = LoadedObservation.of(_obs(ticker))
    with_obs, therm1, doc1 = _build(ticker, tmp_path, with_client=with_client,
                                    observation=loaded, requested=True)
    assert therm0 == therm1
    assert _card(with_obs) == _card(plain)
    assert SECTION_TITLE not in plain and not_produced_line(ticker) not in plain
    assert SECTION_TITLE in with_obs
    # The section is appended to the appendix; everything before it is the
    # plain report, byte for byte.
    assert with_obs.startswith(plain.rstrip("\n"))
    assert with_obs.index(SECTION_TITLE) >= len(plain.rstrip("\n"))
    assert _non_valuation(doc1) == _non_valuation(doc0)
    assert [i.id for i in _non_valuation(doc1)] == [i.id for i in doc0.items]
    assert doc1.unsourced == doc0.unsourced
    assert (doc1.streams, doc1.selections, doc1.coverage, doc1.cik) == (
        doc0.streams, doc0.selections, doc0.coverage, doc0.cik)
    assert doc0.valuation is None
    assert doc1.valuation is not None and doc1.valuation.state == "produced"
    valuation = [i for i in doc1.items if i.plane is Plane.VALUATION]
    assert valuation and all(i.validation_status == "unvalidated" for i in valuation)
    assert any(i.kind == "market_observation" for i in valuation)
    assert any(i.kind == "bridge_component" and i.provenance
               and all(p.kind == "filing" for p in i.provenance) for i in valuation)
    assert doc1.valuation.observation is not None
    assert doc1.valuation.observation.kind == "observation"
    assert doc1.valuation.observation.observation_sha256 == loaded.sha256


def test_requested_without_an_observation_says_not_produced_only_when_asked(tmp_path):
    plain, _, doc0 = _build("KO", tmp_path, with_client=False)
    asked, _, doc1 = _build("KO", tmp_path, with_client=False, requested=True)
    assert not_produced_line("KO") not in plain and doc0.valuation is None
    assert not_produced_line("KO") in asked and SECTION_TITLE not in asked
    assert "scripts/market.py record" in not_produced_line("KO")
    assert doc1.valuation is not None
    assert doc1.valuation.state == "not produced: no market observation"
    assert doc1.valuation.observation is None
    assert [i for i in doc1.items if i.plane is Plane.VALUATION] == []
    assert _card(asked) == _card(plain)
    assert asked.startswith(plain.rstrip("\n"))


def test_the_section_keeps_the_three_data_classes_apart(tmp_path):
    report, _, _ = _build("KO", tmp_path, with_client=False,
                          observation=LoadedObservation.of(_obs()), requested=True)
    section = report.split(SECTION_TITLE)[1]
    assert "This section does not feed any score or flag." in section
    heads = [h for h in ("### Market observation", "### Filing-derived facts",
                         "### Model assumptions") if h in section]
    assert heads == ["### Market observation", "### Filing-derived facts",
                     "### Model assumptions"]
    assert section.index(heads[0]) < section.index(heads[1]) < section.index(heads[2])
    assert "66.25 USD" in section and "2026-10-02T21:00:00+00:00" in section
    assert "NYSE official close (broker statement)" in section
    assert "age 1 day" in section and "STALE" not in section
    assert "default assumptions (not operator-supplied)" in section
    assert "own-history range: not available" in section
    assert "peer range: no reference class (none defined)" in section
    assert "no scenarios recorded" in section
    assert "not in EV (lessee comparability caveat)" in section


def test_stale_marker_after_seven_days(tmp_path):
    eight = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
    report, _, _ = _build("KO", tmp_path, with_client=False,
                          observation=LoadedObservation.of(_obs(observed_at=eight)), requested=True)
    section = report.split(SECTION_TITLE)[1]
    assert "STALE" in section and "age 8 days" in section
    seven = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    report, _, _ = _build("KO", tmp_path, with_client=False,
                          observation=LoadedObservation.of(_obs(observed_at=seven)), requested=True)
    assert "STALE" not in report.split(SECTION_TITLE)[1]


def test_an_observation_for_another_ticker_is_refused(tmp_path):
    with pytest.raises(ValueError, match="AAPL"):
        _build("KO", tmp_path, with_client=False,
               observation=LoadedObservation.of(_obs("AAPL")), requested=True)


# --- the entry points: discovery under journal/market, fail closed ------------------


@pytest.fixture
def sec(tmp_path, monkeypatch):
    """An SEC cache seeded as the drill seeds it, read by the real client with
    the network shut; the journal's reports and market folder under tmp_path."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(journal_reporting, "MARKET", tmp_path / "journal" / "market")

    def no_network(self, url):
        raise sec_client.SecClientError(f"test: no network ({url})")

    monkeypatch.setattr(sec_client.SecClient, "_get", no_network)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: sec_client.SecClient(
        cache_dir=cache, identity="Valuation Test test@example.com"))

    def seed(ticker: str) -> Path:
        cik = CIKS[ticker]
        cache.mkdir(exist_ok=True)
        (cache / "company_tickers.json").write_text(json.dumps(
            {"0": {"cik_str": cik, "ticker": ticker, "title": TITLES[ticker]}}))
        (cache / f"companyfacts_CIK{cik:010d}.json").write_text(json.dumps(_facts(ticker)))
        (cache / f"submissions_CIK{cik:010d}.json").write_text(
            json.dumps(drill._index(cik, ticker, date.today())))
        return cache

    return seed


def _live_reports(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "reports").glob("*.md")] if (tmp_path / "reports").is_dir() else []


def test_journal_build_reads_the_observation_from_journal_market(sec, tmp_path):
    sec("KO")
    write_observation(tmp_path / "journal", _obs_now())
    out, _ = journal_reporting.build_report("KO", with_docs=False, vintage=False)
    text = out.read_text()
    assert SECTION_TITLE in text
    doc = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    assert doc.valuation is not None and doc.valuation.state == "produced"
    # --no-market: no section and no "not produced" line either.
    out2, _ = journal_reporting.build_report("KO", with_docs=False, vintage=False, market=False)
    text2 = out2.read_text()
    assert SECTION_TITLE not in text2 and not_produced_line("KO") not in text2
    doc2 = LedgerDocument.model_validate_json(ledger_path(out2).read_text())
    assert doc2.valuation is None


def test_journal_build_without_an_observation_says_so(sec, tmp_path):
    sec("KO")
    out, _ = journal_reporting.build_report("KO", with_docs=False, vintage=False)
    assert not_produced_line("KO") in out.read_text()
    doc = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    assert doc.valuation is not None and doc.valuation.state.startswith("not produced")


def test_journal_build_fails_closed_on_an_invalid_observation_file(sec, tmp_path):
    sec("KO")
    path = observation_path(tmp_path / "journal", "KO")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(NotPublished) as e:
        journal_reporting.build_report("KO", with_docs=False, vintage=False)
    assert str(path) in str(e.value)
    assert _live_reports(tmp_path) == []
    assert not list((tmp_path / "reports").rglob("*.ledger.json")) if (tmp_path / "reports").exists() else True


def test_a_replay_never_carries_a_present_day_observation(sec, tmp_path):
    from app.services.ingestion import vintages

    sec("KO")
    write_observation(tmp_path / "journal", _obs_now())
    vintages.store_snapshot(CIKS["KO"], {"cik": CIKS["KO"], **_facts("KO")},
                            now=datetime.now(UTC) - timedelta(days=1))
    out, _ = journal_reporting.build_report(
        "KO", with_docs=False, report_day=date.today().isoformat(), replay=True)
    text = out.read_text()
    assert out.name.endswith(".replay.md")
    assert SECTION_TITLE not in text and not_produced_line("KO") not in text


def test_the_cli_fails_closed_and_names_the_file(sec, tmp_path, monkeypatch, capsys):
    from scripts import generate_report

    cache = sec("AAPL")
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: sec_client.SecClient(
        cache_dir=cache, identity="Valuation Test test@example.com"))
    monkeypatch.setattr("sys.argv", ["generate_report.py", "AAPL", "--no-docs", "--no-vintage"])
    # No observation recorded: published, and stdout says the card was not produced.
    assert generate_report._main() == 0
    assert "valuation shadow card: not produced (no market observation recorded for AAPL)" in (
        capsys.readouterr().out)
    (live,) = _live_reports(tmp_path)
    before = live.read_bytes()
    path = observation_path(tmp_path / "journal", "AAPL")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ticker": "AAPL", "price": -1}))
    assert generate_report._main() == 3
    err = capsys.readouterr().err
    assert str(path) in err and "no report published" in err
    # Nothing published: the earlier run is still the live one, byte for byte.
    assert _live_reports(tmp_path) == [live] and live.read_bytes() == before
    # A valid observation: the section is in the published report.
    write_observation(tmp_path / "journal", _obs_now("AAPL"))
    assert generate_report._main() == 0
    assert f"valuation shadow card: produced from {path}" in capsys.readouterr().out
    (report,) = _live_reports(tmp_path)
    assert SECTION_TITLE in report.read_text()
    # --no-market: untouched by the file, and stdout says nothing of the card.
    monkeypatch.setattr("sys.argv", ["generate_report.py", "AAPL", "--no-docs", "--no-vintage",
                                     "--no-market"])
    assert generate_report._main() == 0
    assert "valuation shadow card" not in capsys.readouterr().out
    (report,) = _live_reports(tmp_path)
    text = report.read_text()
    assert SECTION_TITLE not in text and not_produced_line("AAPL") not in text


# --- scripts/market.py --------------------------------------------------------------


@pytest.fixture
def market(tmp_path, monkeypatch):
    from scripts import market as market_cli

    monkeypatch.setattr(market_cli, "ROOT", tmp_path)
    return market_cli


def test_market_record_show_remove(market, tmp_path, capsys):
    at = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    assert market.main(["record", "ko", "--price", "66.25", "--at", at,
                        "--source", "NYSE official close", "--note", "after the print",
                        "--required-return", "0.1", "--terminal-growth", "0.02",
                        "--horizon-years", "8", "--scenario", "bull:0.10:5",
                        "--scenario", "bear:-0.05:3:0.0:0.12", "--scenario", "flat:0:2:0.01"]) == 0
    path = observation_path(tmp_path / "journal", "KO")
    assert path.is_file() and str(path) in capsys.readouterr().out
    doc = json.loads(path.read_text())
    assert doc["ticker"] == "KO" and doc["price"] == 66.25 and doc["currency"] == "USD"
    assert doc["assumptions"] == {"required_return": 0.1, "terminal_growth": 0.02,
                                  "horizon_years": 8}
    assert [s["name"] for s in doc["scenarios"]] == ["bull", "bear", "flat"]
    assert doc["scenarios"][2] == {"name": "flat", "fcf_growth": 0.0, "years": 2,
                                   "terminal_growth": 0.01, "required_return": None}
    assert doc["scenarios"][1] == {"name": "bear", "fcf_growth": -0.05, "years": 3,
                                   "terminal_growth": 0.0, "required_return": 0.12}
    assert doc["recorded_at"] >= doc["observed_at"]
    assert market.main(["show", "KO"]) == 0
    out = capsys.readouterr().out
    assert "66.25" in out and "age" in out and "STALE" not in out
    assert market.main(["remove", "KO"]) == 0
    assert not path.exists()
    assert market.main(["show", "KO"]) == 2
    assert "no market observation recorded for KO" in capsys.readouterr().err
    assert market.main(["remove", "KO"]) == 2


def test_market_show_counts_one_day_in_the_singular(market, capsys):
    at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert market.main(["record", "KO", "--price", "1", "--at", at, "--source", "s"]) == 0
    assert market.main(["show", "KO"]) == 0
    assert "age: 1 day as of" in capsys.readouterr().out


def test_market_show_marks_a_stale_observation(market, capsys):
    at = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    assert market.main(["record", "KO", "--price", "1", "--at", at, "--source", "s"]) == 0
    assert market.main(["show", "KO"]) == 0
    assert "STALE" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["record", "KO", "--price", "0", "--at", "2026-10-02T21:00:00+00:00", "--source", "s"],
    ["record", "KO", "--price", "nan", "--at", "2026-10-02T21:00:00+00:00", "--source", "s"],
    ["record", "KO", "--price", "1", "--at", "2026-10-02T21:00:00", "--source", "s"],  # naive
    ["record", "KO", "--price", "1", "--at", "2999-01-01T00:00:00+00:00", "--source", "s"],
    ["record", "KO", "--price", "1", "--at", "2026-10-02T21:00:00+00:00", "--source", ""],
    ["record", "KO", "--price", "1", "--at", "2026-10-02T21:00:00+00:00", "--source", "s",
     "--currency", "usd"],
    ["record", "KO", "--price", "1", "--at", "2026-10-02T21:00:00+00:00", "--source", "s",
     "--scenario", "bull:fast:5"],
    ["record", "KO", "--price", "1", "--at", "2026-10-02T21:00:00+00:00", "--source", "s",
     "--terminal-growth", "0.5"],
    ["record", "../KO", "--price", "1", "--at", "2026-10-02T21:00:00+00:00", "--source", "s"],
    ["show", "../KO"],
])
def test_market_invalid_input_exits_2_and_writes_nothing(market, tmp_path, argv, capsys):
    assert market.main(argv) == 2
    assert capsys.readouterr().err.strip()
    assert not (tmp_path / "journal" / "market").exists() or not list(
        (tmp_path / "journal" / "market").iterdir())


def test_market_io_failure_exits_1(market, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal" / "market").symlink_to(outside)
    at = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    assert market.main(["record", "KO", "--price", "1", "--at", at, "--source", "s"]) == 1
    assert "symlink" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
