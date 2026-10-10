"""The valuation plane in the evidence ledger and the review console: the
price row rests on the operator's observation (a third provenance kind),
the bridge rows on filings, the derived rows on those; a ledger written
before the plane existed still loads; the console lists the rows apart and
never offers to reconcile the observation to an accession."""

from __future__ import annotations

import csv
import functools
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.pipeline import analyze
from app.schemas.ledger import (
    LedgerDocument,
    Plane,
    Provenance,
    ValidationStatus,
    ValuationSummary,
)
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.journal import reporting, review, store
from app.services.reporting.ledger import build_ledger
from app.services.reporting.report_files import (
    ENGINE_ENV,
    engine_commit,
    recording,
    replacing,
)
from app.services.valuation.observation import LoadedObservation, MarketObservation
from app.services.valuation.plane import compute_plane
from app.services.watch import watchlist as wl
from app.web import app

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DAY = date(2026, 10, 3)
OBSERVED = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
OBS = MarketObservation(ticker="KO", price=66.25, currency="USD", observed_at=OBSERVED,
                        source="NYSE official close", recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC))
SHA = "ab" * 32


def _observation_provenance(**kw) -> Provenance:
    base = dict(kind="observation", observed_at=OBSERVED, source="NYSE official close",
                recorded_at=datetime(2026, 10, 3, 9, tzinfo=UTC), observation_sha256=SHA)
    return Provenance(**{**base, **kw})


# --- the schema ---------------------------------------------------------------------


def test_observation_provenance_round_trips_json():
    p = _observation_provenance(role="price")
    assert Provenance.model_validate_json(p.model_dump_json()) == p
    assert p.accession is None and p.url is None


@pytest.mark.parametrize("missing", ["observed_at", "source", "recorded_at", "observation_sha256"])
def test_observation_provenance_needs_its_four_fields(missing):
    with pytest.raises((ValidationError, ValueError), match=missing):
        _observation_provenance(**{missing: None})


def test_filing_and_snapshot_rules_are_unchanged():
    with pytest.raises(ValueError, match="filing provenance needs"):
        Provenance(kind="filing", accession="x")
    with pytest.raises(ValueError, match="snapshot provenance needs"):
        Provenance(kind="snapshot")
    assert Provenance(kind="snapshot", snapshot_sha256="a" * 64, captured=DAY).kind == "snapshot"


FILING = dict(kind="filing", accession="0000021344-26-000010", form="10-Q", filed=date(2026, 4, 30))


@pytest.mark.parametrize("field,value", [
    ("observed_at", OBSERVED), ("source", "NYSE"),
    ("recorded_at", datetime(2026, 10, 3, 9, tzinfo=UTC)), ("observation_sha256", SHA),
])
@pytest.mark.parametrize("base", [FILING, dict(kind="snapshot", snapshot_sha256="a" * 64, captured=DAY)],
                         ids=["filing", "snapshot"])
def test_observation_fields_are_refused_on_the_other_kinds(base, field, value):
    # Review of 48b1f04, F6: the validator was one-directional.
    with pytest.raises(ValueError, match=f"{base['kind']} provenance does not carry {field}"):
        Provenance(**{**base, field: value})


@pytest.mark.parametrize("field,value", [
    ("accession", "0000021344-26-000010"), ("form", "10-Q"), ("filed", date(2026, 4, 30)),
    ("concept", "us-gaap:Cash"), ("period_start", date(2026, 1, 1)), ("period_end", date(2026, 3, 31)),
    ("snapshot_sha256", "a" * 64), ("captured", DAY),
])
def test_filing_and_snapshot_fields_are_refused_on_an_observation(field, value):
    with pytest.raises(ValueError, match=f"observation provenance does not carry {field}"):
        _observation_provenance(**{field: value})


@pytest.mark.parametrize("field", ["observed_at", "recorded_at"])
def test_observation_times_must_be_aware(field):
    with pytest.raises(ValidationError, match="timezone"):
        _observation_provenance(**{field: datetime(2026, 10, 2, 21)})
    raw = json.loads(_observation_provenance().model_dump_json())
    raw[field] = "2026-10-02T21:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Provenance.model_validate(raw)


def test_plane_and_summary():
    assert Plane.VALUATION == "valuation"
    s = ValuationSummary(state="produced", ev=1.0, ev_reason=None, fiscal_label="FY2026Q2",
                         observation=_observation_provenance(),
                         availability="filing-derived facts as filed by 2026-10-01: FY2026Q2")
    assert ValuationSummary.model_validate_json(s.model_dump_json()) == s
    # Two states only: a plane that cannot be computed fails the build
    # closed and writes no ledger at all (F8).
    assert "not produced: <error>" not in (ValuationSummary.__doc__ or "")


@functools.cache
def _ko():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    return ds, analyze(ds)


def _plane():
    ds, _ = _ko()
    return compute_plane(ds, LoadedObservation.of(OBS), DAY)


def _ledger(valuation=None, requested: bool = False) -> LedgerDocument:
    ds, result = _ko()
    return build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY,
                        cik_sources={"the companyfacts payload": 21344},
                        valuation=valuation, valuation_requested=requested)


def test_a_ledger_written_before_the_plane_still_loads():
    doc = _ledger()
    raw = json.loads(doc.model_dump_json())
    assert raw["valuation"] is None
    del raw["valuation"]
    for item in raw["items"]:
        for p in item["provenance"]:
            for key in ("observed_at", "source", "recorded_at", "observation_sha256"):
                p.pop(key, None)
    loaded = LedgerDocument.model_validate(raw)
    assert loaded.valuation is None
    assert loaded.items == doc.items


def test_valuation_rows_rest_on_the_observation_the_filings_and_each_other():
    plane = _plane()
    doc = _ledger(plane, requested=True)
    rows = [i for i in doc.items if i.plane is Plane.VALUATION]
    assert rows and all(i.validation_status is ValidationStatus.UNVALIDATED for i in rows)
    by_kind: dict[str, list] = {}
    for i in rows:
        by_kind.setdefault(i.kind, []).append(i)
    (price,) = by_kind["market_observation"]
    (p,) = price.provenance
    assert p.kind == "observation" and p.observation_sha256 == plane.loaded.sha256
    assert p.source == OBS.source and p.observed_at == OBSERVED
    assert price.value == 66.25 and "USD" in price.claim
    # Each filing-derived bridge line names exactly the facts behind the field.
    ds, _ = _ko()
    period = next(x for x in ds.periods if x.fiscal_label == plane.bridge.fiscal_label)
    components = {i.subject: i for i in by_kind["bridge_component"]}
    assert set(components) == {c.name for c in plane.bridge.filing_components()}
    for name, item in components.items():
        refs = period.sources[name].inputs
        assert [(q.accession, q.concept, q.value) for q in item.provenance] == [
            (r.accession, r.concept, r.value) for r in refs]
        assert item.fiscal_label == period.fiscal_label and item.value == getattr(period, name)
    (mcap,) = by_kind["market_cap"]
    assert set(mcap.derived_from) == {price.id, components["shares_outstanding"].id}
    (ev,) = by_kind["enterprise_value"]
    assert ev.value == plane.bridge.ev
    assert mcap.id in ev.derived_from and components["total_debt"].id in ev.derived_from
    assert "assumed 0" in (ev.note or "")
    # The TTM figures, each on the four quarters' filings, under the multiples.
    ttm = {i.subject: i for i in by_kind["ttm_figure"]}
    assert set(ttm) == {"revenue", "net_income", "ebit", "ebitda", "fcf"}
    for name, item in ttm.items():
        assert item.provenance and item.fiscal_label == plane.ttm.label
        assert item.value == getattr(plane.ttm, name)
        assert len(item.provenance) >= 4  # one filing per quarter at least
    multiples = {m.subject: m for m in by_kind["multiple"]}
    assert set(multiples) == {m.name for m in plane.multiples}
    assert set(multiples["P/E"].derived_from) == {mcap.id, ttm["net_income"].id}
    assert set(multiples["EV/EBITDA"].derived_from) == {ev.id, ttm["ebitda"].id}
    assert set(multiples["FCF yield"].derived_from) == {mcap.id, ttm["fcf"].id}
    growth = {i.subject: i for i in by_kind["implied_growth"]}
    # The solves are against the market cap (F7): they rest on it, not on EV.
    assert set(growth["reverse_dcf"].derived_from) == {mcap.id, ttm["fcf"].id}
    assert set(growth["gordon"].derived_from) == {mcap.id, ttm["fcf"].id}
    assert growth["gordon"].formula == "g = r − FCF_ttm / market cap"
    assert growth["gordon"].note == "default assumptions (not operator-supplied): r=9.0%"
    assert growth["reverse_dcf"].note.startswith(
        "default assumptions (not operator-supplied): r=9.0%, terminal 2.5%, 10 years; "
        "main assumption: ")
    assert doc.valuation is not None and doc.valuation.state == "produced"
    assert doc.valuation.ev == plane.bridge.ev
    assert doc.valuation.availability == plane.bridge.availability
    assert doc.valuation.observation == p.model_copy(update={"role": None})
    # The whole document survives JSON (F3: an inf used to serialize as null).
    assert LedgerDocument.model_validate_json(doc.model_dump_json()) == doc
    # Nothing of the plane is listed as unsourced, and the other planes are as before.
    assert [u for u in doc.unsourced if u.plane is Plane.VALUATION] == []
    plain = _ledger()
    assert [i for i in doc.items if i.plane is not Plane.VALUATION] == plain.items


def test_a_ttm_figure_the_window_cannot_build_has_no_row():
    """A dataset without D&A (no EBITDA) and without provenance: the four
    figures that exist are listed (unsourced, no per-value provenance),
    EBITDA is neither a row nor an unsourced claim."""
    from datetime import date as _date

    from app.schemas.financials import (
        CompanyDataset,
        CompanyProfile,
        PeriodFinancials,
        PeriodType,
    )

    ends = (_date(2025, 3, 31), _date(2025, 6, 30), _date(2025, 9, 30), _date(2025, 12, 31))
    periods = [PeriodFinancials(period_end=e, period_type=PeriodType.QUARTER, fiscal_label=f"FY2025Q{i + 1}",
                                revenue=1000.0, net_income=100.0, ebit=150.0, cfo=200.0, capex=50.0,
                                cash_and_equivalents=100.0, total_debt=300.0, shares_outstanding=10.0)
               for i, e in enumerate(ends)]
    ds = CompanyDataset(profile=CompanyProfile(ticker="SYN"), periods=periods)
    obs = OBS.model_copy(update={"ticker": "SYN"})
    plane = compute_plane(ds, LoadedObservation.of(obs), DAY)
    assert plane.ttm.ebitda is None
    doc = build_ledger(result=analyze(ds), dataset=ds, ticker="SYN", report_date=DAY, valuation=plane)
    assert not [i for i in doc.items if i.kind == "ttm_figure"]
    unsourced = {u.subject for u in doc.unsourced if u.kind == "ttm_figure"}
    assert unsourced == {"revenue", "net_income", "ebit", "fcf"}


def test_not_produced_states():
    assert _ledger(None, requested=False).valuation is None
    doc = _ledger(None, requested=True)
    assert doc.valuation == ValuationSummary(state="not produced: no market observation")


def test_a_scenario_row_and_the_ttm_window_read_the_as_filed_dataset():
    """With the raw facts the plane maps its own point-in-time dataset (F1);
    the ledger's TTM rows cite that dataset's filings, and a scenario rests
    on the price, the share count and TTM FCF — not on EV (F7)."""
    from app.services.valuation.observation import Scenario

    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, result = _ko()
    obs = OBS.model_copy(update={"observed_at": datetime(2026, 2, 21, 21, tzinfo=UTC),
                                 "scenarios": (Scenario(name="s", fcf_growth=0.05, years=5),)})
    plane = compute_plane(ds, LoadedObservation.of(obs), DAY, company_facts=facts)
    assert plane.as_filed_by == date(2026, 2, 20) and plane.bridge.fiscal_label == "FY2025Q4"
    assert plane.dataset is not ds
    doc = build_ledger(result=result, dataset=ds, ticker="KO", report_date=DAY,
                       valuation=plane, valuation_requested=True)
    rows = [i for i in doc.items if i.plane is Plane.VALUATION]
    for item in rows:
        assert all(p.kind != "filing" or p.filed <= date(2026, 2, 20) for p in item.provenance), item.id
    ttm = {i.subject: i for i in rows if i.kind == "ttm_figure"}
    assert ttm["fcf"].fiscal_label == plane.ttm.label == "TTM FY2025Q4"
    (sc,) = [i for i in rows if i.kind == "scenario"]
    price = next(i for i in rows if i.kind == "market_observation")
    shares = next(i for i in rows if i.kind == "bridge_component" and i.subject == "shares_outstanding")
    assert set(sc.derived_from) == {price.id, shares.id, ttm["fcf"].id}
    assert sc.value == plane.expectations.scenarios[0].value_per_share
    assert doc.valuation.fiscal_label == "FY2025Q4"
    assert doc.valuation.availability.startswith("filing-derived facts as filed by 2026-02-20")
    assert LedgerDocument.model_validate_json(doc.model_dump_json()) == doc


# --- the console ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    monkeypatch.setenv(ENGINE_ENV, "0123456789ab")
    engine_commit.cache_clear()
    yield
    engine_commit.cache_clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "journal" / "entries")
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(wl, "WATCHLIST", tmp_path / "journal" / "watchlist.json")
    store.ENTRIES.mkdir(parents=True)
    reporting.REPORTS.mkdir()
    return tmp_path


@pytest.fixture
def client(home):
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                      follow_redirects=False)


def _publish(day: str, doc: LedgerDocument) -> str:
    out = reporting.report_path("KO", day)
    with recording() as made, replacing(out) as staged:
        staged.report.write_text("# KO report\n\n| Block | Score |\n|---|---|\n| EQ | 29 |\n")
        staged.ledger.write_text(doc.model_dump_json())
    return made[-1].generation_id


def _entry() -> str:
    path = store.open_entry("KO", "a thesis", 3, "hold")
    return path.stem.split("_", 1)[1]


def _row(html: str, key: str) -> str:
    m = re.search(rf'<tr id="{key}".*?</tr>', html, re.S)
    assert m, f"no row {key}"
    return m.group(0)


def _tick(client, day, gid, key):
    return client.post("/review/KO/reconcile", data={
        "date": day, "generation": gid, "key": key, "state": "reconciled", "note": ""})


def test_the_case_page_lists_valuation_rows_apart(client):
    day = _entry()
    doc = _ledger(_plane(), requested=True)
    gid = _publish(day, doc)
    page = client.get(f"/review/KO?date={day}").text
    assert "Valuation (shadow)" in page
    price = next(i for i in doc.items if i.kind == "market_observation")
    shares = next(i for i in doc.items if i.kind == "bridge_component"
                  and i.subject == "shares_outstanding")
    mcap = next(i for i in doc.items if i.kind == "market_cap")
    main_table = page.split("Valuation (shadow)")[0]
    for key in (price.id, shares.id, mcap.id):
        assert f'id="{key}"' not in main_table
    section = page.split("Valuation (shadow)")[1]
    row = _row(section, price.id)
    assert "market observation · NYSE official close · observed 2026-10-02T21:00:00+00:00" in row
    assert "sec.gov" not in row and "<form" not in row
    assert "not reconcilable" in row
    # A filing-backed bridge row is reconcilable like any other filing row.
    row = _row(section, shares.id)
    assert "sec.gov/Archives/edgar/data/21344/" in row and "<form" in row
    assert mcap.id in section and "rests on" in section
    # The main count is of the accounting rows only.
    case = review.case("KO", day)
    assert len(case.rows) == len([i for i in doc.items if i.provenance
                                  and i.plane is not Plane.VALUATION])
    assert {r.key for r in case.valuation} == {i.id for i in doc.items
                                               if i.plane is Plane.VALUATION and i.provenance}
    assert {i.id for i in case.valuation_derived} == {i.id for i in doc.items
                                                      if i.plane is Plane.VALUATION
                                                      and not i.provenance}
    # The observation row cannot be ticked; a filing-backed bridge row can.
    r = _tick(client, day, gid, price.id)
    assert r.status_code == 404
    assert _tick(client, day, gid, shares.id).status_code == 303


def test_every_tick_control_is_labelled_and_every_id_is_unique(client):
    """Hermes audit of PR #118 (findings 3 and 6): the review console's
    selects, note boxes and Save buttons had no accessible name (a screen
    reader read "combo box", "edit text", "Save" for every row of the
    table), and a page's ids must be unique. Each control names its row."""
    from html.parser import HTMLParser

    day = _entry()
    doc = _ledger(_plane(), requested=True)
    _publish(day, doc)
    page = client.get(f"/review/KO?date={day}").text

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids, self.controls, self.labels = [], [], set()

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if a.get("id"):
                self.ids.append(a["id"])
            if tag == "label" and a.get("for"):
                self.labels.add(a["for"])
            if (tag in ("select", "textarea", "button")
                    or (tag == "input" and a.get("type") != "hidden")):
                self.controls.append((tag, a))

    parsed = Controls()
    parsed.feed(page)
    assert sorted({i for i in parsed.ids if parsed.ids.count(i) > 1}) == []
    forms = page.count('class="tick-form"')
    assert forms >= 2 and "Valuation (shadow)" in page
    tick = [(t, a) for t, a in parsed.controls if a.get("name") in ("state", "note")
            or (t == "button" and a.get("type") == "submit")]
    assert len(tick) == 3 * forms
    for tag, a in tick:
        named = a.get("aria-label") or (a.get("id") in parsed.labels)
        assert named, (tag, a)
    names = {a.get("aria-label") for _, a in tick}
    shares = next(i for i in doc.items if i.kind == "bridge_component"
                  and i.subject == "shares_outstanding")
    assert f"State of {shares.id}" in names and f"Note on {shares.id}" in names
    assert f"Save the tick of {shares.id}" in names


def test_the_export_includes_the_valuation_rows(client):
    day = _entry()
    doc = _ledger(_plane(), requested=True)
    _publish(day, doc)
    price = next(i for i in doc.items if i.kind == "market_observation")
    shares = next(i for i in doc.items if i.kind == "bridge_component"
                  and i.subject == "shares_outstanding")
    rows = list(csv.DictReader(io.StringIO(client.get(f"/review/KO/export?date={day}").text)))
    by_key = {r["key"]: r for r in rows}
    assert price.id in by_key and by_key[price.id]["accessions"] == ""
    assert by_key[price.id]["kind"] == "market_observation"
    assert shares.id in by_key and by_key[shares.id]["accessions"]
    md = client.get(f"/review/KO/export?date={day}&format=md").text
    assert price.id in md and shares.id in md and "## Valuation (shadow)" in md


def test_a_ledger_without_valuation_rows_shows_no_section(client):
    day = _entry()
    _publish(day, _ledger())
    page = client.get(f"/review/KO?date={day}").text
    assert "Valuation (shadow)" not in page


def test_the_console_shows_the_unit_beside_each_monetary_value(client):
    """Hermes re-audit of #118 @ 34836cf: the console printed bare numbers;
    the ledger now states each monetary row's currency and each filed
    fact's unit, and the console shows them."""
    day = _entry()
    doc = _ledger(_plane(), requested=True)
    _publish(day, doc)
    page = client.get(f"/review/KO?date={day}").text
    section = page.split("Valuation (shadow)")[1]
    debt = next(i for i in doc.items if i.kind == "bridge_component" and i.subject == "total_debt")
    def shown(row: str) -> str:
        return re.search(r'<span style="font-family:var\(--mono\)">(.*?)</span>', row).group(1)

    row = _row(section, debt.id)
    assert shown(row) == f"{review.fmt_value(debt.value)} USD"
    filed = debt.provenance[0]
    assert f"{review.fmt_value(filed.value)} USD" in row  # the fact as filed, with its unit
    shares = next(i for i in doc.items if i.kind == "bridge_component"
                  and i.subject == "shares_outstanding")
    row = _row(section, shares.id)
    assert shown(row) == f"{review.fmt_value(shares.value)} shares"
    price = next(i for i in doc.items if i.kind == "market_observation")
    assert shown(_row(section, price.id)) == f"{review.fmt_value(price.value)} USD"


def test_a_row_of_mixed_units_shows_none_and_the_case_answers_head(client):
    from app.schemas.ledger import EvidenceItem
    from app.schemas.ledger import Provenance as P

    def filed(unit):
        return P(kind="filing", accession="0000021344-26-000010", form="10-Q",
                 filed=date(2026, 4, 28), unit=unit)

    item = EvidenceItem(id="EV-x", plane=Plane.VALUATION, kind="k", subject="s", claim="c",
                        provenance=(filed("USD"), filed("shares")),
                        validation_status="unvalidated")
    assert review._unit(item) is None
    assert review._unit(item.model_copy(update={"provenance": (filed("USD"),)})) == "USD"
    assert review._unit(item.model_copy(update={"currency": "EUR"})) == "EUR"
    # Only filed facts carry a unit: an observation beside one says nothing.
    seen = P(kind="observation", observed_at=datetime(2026, 10, 2, 21, tzinfo=UTC),
             source="close", recorded_at=datetime(2026, 10, 3, tzinfo=UTC),
             observation_sha256="0" * 64)
    assert review._unit(item.model_copy(update={"provenance": (seen, filed("USD"))})) == "USD"
    day = _entry()
    _publish(day, _ledger(_plane(), requested=True))
    assert client.head(f"/review/KO?date={day}").status_code == 200
