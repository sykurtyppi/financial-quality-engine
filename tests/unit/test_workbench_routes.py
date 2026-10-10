"""The workbench pages (r36): a ticker in, a decision card out.

Before r36 there was no way in: `/` was the journal's dashboard, there was
no ticker input, and `/report/{T}` built only for legacy v1 journal entries
that can no longer be created. The workbench is a local, single-user front
end over the same publish path the CLI uses (`reporting.build_report`) and
the same files (`reports/`, `journal/market/`, `journal/watchlist.json`).

Offline throughout: a fast publishing stand-in where the build itself is not
the point, and the REAL build (`test_the_real_offline_build_end_to_end...`)
on the KO fixture seeded as the drill seeds an SEC cache, with the network
shut, end to end through the routes.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.ledger import LedgerDocument
from app.services.brief import sources as brief_sources
from app.services.ingestion import sec_client
from app.services.journal import reporting, store
from app.services.reporting.report_builder import ledger_path
from app.services.reporting.report_files import read_live, replacing
from app.services.valuation.observation import observation_path
from app.services.valuation.render import SECTION_TITLE as _SECTION_MD
from app.services.watch import watchlist as wl
from app.services.workbench import jobs, watching
from app.web import app
from scripts import drill

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
# The shadow card's heading, as the page renders it.
SECTION_TITLE = _SECTION_MD.lstrip("# ")
IDENTITY = "Workbench Test test@example.com"
CARD = """# Decision Card — {t}

_As of {day}. 90-second triage; full report follows as appendix._

## Attention flags

**Tier 1 — validated (low false-positive):**
- ⚠ not checked this run: silent revisions (no vintage baseline yet) (see data quality)

**Tier 2 — directional (review in context):**
- Elevated concern: leverage_change (FY2027Q1)

**Tier 3 — context:**
- none surfaced this run

## Scope limitation

**Examples of material risks not analyzed by this engine include:** a list. {n}

---

# Full report (appendix)

## Block scores

| Block | Score |
|---|---|
| Earnings Quality | 29 |

Risk factor quoted from a filing: <script>alert(1)</script>
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(reporting, "MARKET", tmp_path / "journal" / "market")
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "journal" / "entries")
    monkeypatch.setattr(wl, "WATCHLIST", tmp_path / "journal" / "watchlist.json")
    monkeypatch.setattr(brief_sources, "BRIEFS", tmp_path / "reports" / "briefs")
    monkeypatch.setattr(jobs, "REGISTRY", jobs.Registry())
    monkeypatch.setenv("EDGAR_IDENTITY", IDENTITY)
    (tmp_path / "journal").mkdir()
    return tmp_path


@pytest.fixture
def client(env):
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                      follow_redirects=False)


@pytest.fixture
def fake_build(monkeypatch):
    """A publishing stand-in for `build_report`: one generation per call,
    through `replacing`, fenced with the run's request number, as the real
    build publishes. Returns the calls."""
    calls: list[dict] = []

    def build(ticker, with_docs=True, report_day=None, fresh=False, out_dir=None, fence=None,
              **kw):
        calls.append({"ticker": ticker, "fresh": fresh})
        # Where the real build writes: ``out_dir`` when given (a workbench
        # run's ``reports/workbench/``), else the journal's ``reports/``.
        day = report_day or date.today().isoformat()
        out = (out_dir or reporting.REPORTS) / f"{store.safe_ticker(ticker)}_{day}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        with replacing(out, fence=fence) as staged:
            staged.report.write_text(CARD.format(t=ticker.upper(), day=date.today(), n=len(calls)))
            staged.ledger.write_text(json.dumps({
                "ticker": ticker.upper(), "generated_on": date.today().isoformat(),
                "config_version": "0.3.0", "items": [], "unsourced": []}))
        return out, "no acute signals"

    monkeypatch.setattr(reporting, "build_report", build)
    return calls


@pytest.fixture
def sec(env, monkeypatch):
    """The real build, offline: an SEC cache seeded from the KO fixture as
    the drill seeds one, read by the real client with `_get` shut (nothing
    reaches the network). `fresh` is honoured, so a "Refresh from SEC"
    here fails as it would with SEC down."""
    cache = env / "cache"
    cache.mkdir()
    cik = 21344
    (cache / "company_tickers.json").write_text(json.dumps(
        {"0": {"cik_str": cik, "ticker": "KO", "title": "COCA COLA CO"}}))
    (cache / f"companyfacts_CIK{cik:010d}.json").write_text(
        (REAL / "companyfacts_KO_trimmed.json").read_text())
    (cache / f"submissions_CIK{cik:010d}.json").write_text(
        json.dumps(drill._index(cik, "KO", date.today())))

    def no_network(self, url):
        raise sec_client.SecClientError(f"test: no network ({url})")

    monkeypatch.setattr(sec_client.SecClient, "_get", no_network)
    monkeypatch.setattr(reporting, "SecClient", lambda *a, fresh=False, **k: sec_client.SecClient(
        cache_dir=cache, identity=IDENTITY, fresh=fresh))
    return cache


def _wait(ticker: str) -> jobs.Job:
    job = jobs.latest(ticker)
    assert job is not None
    done = jobs.wait(job.id, timeout=60)
    assert done is not None and not done.active, done
    return done


def _workbench(ticker: str, day: str | None = None) -> Path:
    """A workbench run's live name: ``reports/workbench/<T>_<day>.md``."""
    return reporting.REPORTS / "workbench" / f"{ticker}_{day or date.today().isoformat()}.md"


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*")}


# --- home -----------------------------------------------------------------------------


def test_home_with_no_runs_shows_the_setup_card_and_the_ticker_box(client, monkeypatch):
    monkeypatch.delenv("EDGAR_IDENTITY")
    r = client.get("/")
    assert r.status_code == 200
    assert "EDGAR_IDENTITY is not set" in r.text and 'class="panel setup"' in r.text
    assert 'action="/t"' in r.text and 'name="ticker"' in r.text
    assert "No runs yet" in r.text and "watchlist is empty" in r.text.lower()
    for href in ('href="/"', 'href="/#watchlist"', 'href="/journal"', 'href="/review"'):
        assert href in r.text


def test_home_without_setup_problems_has_no_setup_card(client):
    r = client.get("/")
    assert r.status_code == 200 and "setup" not in r.text.split("<main", 1)[1]


def test_home_lists_the_watchlist_and_recent_runs(client, fake_build, env):
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    client.post("/t/KO/run", data={"fresh": "0"})
    _wait("KO")
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="watchlist"' in r.text and 'href="/t/KO"' in r.text
    assert "2026-10-21" in r.text
    recent = r.text.split('id="recent"', 1)[1]
    assert date.today().isoformat() in recent and 'href="/t/KO"' in recent


def test_the_journal_dashboard_moved_to_journal(client):
    r = client.get("/journal")
    assert r.status_code == 200
    assert "Dashboard" in r.text and "No cases yet" in r.text


# --- the ticker box -------------------------------------------------------------------


def test_a_valid_ticker_goes_to_its_page(client):
    for typed in ("KO", "ko", "  ko "):
        r = client.post("/t", data={"ticker": typed})
        assert r.status_code == 303 and r.headers["location"] == "/t/KO"


@pytest.mark.parametrize("bad", ["../x", "ko;rm", "", "   ", "A" * 13, "KO/../../etc", "<b>"])
def test_an_invalid_ticker_is_refused_and_touches_nothing(client, env, bad):
    before = _files(env)
    r = client.post("/t", data={"ticker": bad})
    assert r.status_code == 400
    assert "not a ticker" in r.text and 'action="/t"' in r.text
    if bad == "<b>":
        assert "&lt;b&gt;" in r.text  # said back escaped, never as markup
    assert _files(env) == before and len(jobs.REGISTRY) == 0


def test_a_lower_case_ticker_url_is_sent_to_its_canonical_page(client):
    r = client.get("/t/ko")
    assert r.status_code == 303 and r.headers["location"] == "/t/KO"


@pytest.mark.parametrize("bad", ["ko;rm", "A" * 13, "%3Cb%3E"])
def test_an_invalid_ticker_url_is_refused(client, bad):
    for path in (f"/t/{bad}", f"/t/{bad}/status"):
        assert client.get(path).status_code == 400
    assert client.post(f"/t/{bad}/run", data={"fresh": "0"}).status_code == 400
    assert jobs.REGISTRY.active() == [] and len(jobs.REGISTRY) == 0


# --- a ticker page --------------------------------------------------------------------


def test_a_ticker_with_no_run_offers_run(client):
    r = client.get("/t/KO")
    assert r.status_code == 200
    assert "No report yet" in r.text and 'data-kept' not in r.text
    assert re.search(r'<form[^>]*action="/t/KO/run"', r.text)
    assert 'name="fresh" value="0"' in r.text and "Refresh from SEC" not in r.text


def test_run_then_the_card_appendix_and_history(client, fake_build):
    r = client.post("/t/KO/run", data={"fresh": "0"})
    assert r.status_code == 303 and r.headers["location"] == "/t/KO"
    done = _wait("KO")
    assert done.state == jobs.DONE and fake_build == [{"ticker": "KO", "fresh": False}]
    r = client.get("/t/KO")
    assert r.status_code == 200
    card, appendix = r.text.split("<details", 1)
    assert "Decision Card — KO" in card and "Scope limitation" in card
    assert "Earnings Quality" not in card
    assert "Full report (appendix)" in appendix and "<table>" in appendix
    # Filer-quoted text is escaped, as on every rendered report.
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
    # The card's tiers are marked for styling (the markdown is untouched).
    assert 'class="tier tier-1"' in card and 'class="tier-list tier-2"' in card
    assert 'class="not-checked"' in card and 'class="none"' in card
    # Each flag is its own list item, not a run-on paragraph.
    assert "<li>Elevated concern: leverage_change (FY2027Q1)</li>" in card
    assert "- Elevated concern" not in card
    # Not linked to the review console: it reviews the journal's runs, and
    # this run is the workbench's (`reports/workbench/`).
    assert "Open in review console" not in r.text and 'href="/review/KO' not in r.text
    assert "reports/workbench/" in r.text
    assert done.generation_id in r.text
    history = r.text.split('id="history"', 1)[1]
    assert len(re.findall(r'class="run-row', history)) == 1
    assert "Refresh from SEC" in r.text and 'name="fresh" value="1"' in r.text


def test_refresh_passes_fresh_and_a_second_run_is_history_of_two(client, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    first = _wait("KO")
    client.post("/t/KO/run", data={"fresh": "1"})
    second = _wait("KO")
    assert [c["fresh"] for c in fake_build] == [False, True]
    r = client.get("/t/KO")
    history = r.text.split('id="history"', 1)[1]
    assert len(re.findall(r'class="run-row', history)) == 2
    assert f'href="/t/KO/runs/{first.generation_id}"' in history
    old = client.get(f"/t/KO/runs/{first.generation_id}")
    assert old.status_code == 200
    assert "not the live run" in old.text and "Decision Card — KO" in old.text
    assert "a list. 1" in old.text and "a list. 2" not in old.text
    live = client.get(f"/t/KO/runs/{second.generation_id}")
    assert live.status_code == 200 and "not the live run" not in live.text


def test_a_run_page_resolves_only_the_tickers_own_generations(client, fake_build):
    client.post("/t/CRM/run", data={"fresh": "0"})
    crm = _wait("CRM")
    client.post("/t/KO/run", data={"fresh": "0"})
    _wait("KO")
    for gid in ("..%2F..%2Fetc", "%2E%2E", "abc", "0" * 32, crm.generation_id,
                crm.generation_id.upper(), "current"):
        assert client.get(f"/t/KO/runs/{gid}").status_code == 404, gid


def test_htmx_style_run_returns_the_status_fragment(client, fake_build):
    r = client.post("/t/KO/run", data={"fresh": "0"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "data-job-status" in r.text
    _wait("KO")


def test_status_fragment_reports_running_then_done(client, monkeypatch, env):
    import threading
    gate, entered = threading.Event(), threading.Event()

    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        entered.set()
        gate.wait(10)
        raise sec_client.SecClientError("HTTP 503")

    monkeypatch.setattr(reporting, "build_report", build)
    client.post("/t/KO/run", data={"fresh": "1"})
    assert entered.wait(10)
    page = client.get("/t/KO").text
    assert 'data-state="running"' in page and 'data-status-url="/t/KO/status"' in page
    assert "/static/app.js" in page
    frag = client.get("/t/KO/status")
    assert frag.status_code == 200 and 'data-state="running"' in frag.text
    assert "<html" not in frag.text
    gate.set()
    _wait("KO")
    frag = client.get("/t/KO/status")
    assert 'data-state="failed"' in frag.text and "SEC fetch failed: HTTP 503" in frag.text
    assert "Traceback" not in frag.text


def test_status_with_no_job_is_an_idle_fragment(client):
    r = client.get("/t/KO/status")
    assert r.status_code == 200 and 'data-state="idle"' in r.text


# --- the price box --------------------------------------------------------------------


def _price_form(**over) -> dict:
    observed = (datetime.now(UTC) - timedelta(hours=1)).astimezone().isoformat(timespec="minutes")
    form = {"price": "66.25", "currency": "USD", "observed_at": observed,
            "source": "NYSE official close (broker statement)", "note": ""}
    form.update(over)
    return form


def test_a_valid_price_is_written_and_starts_a_run(client, env, fake_build):
    r = client.post("/t/KO/price", data=_price_form())
    assert r.status_code == 303 and r.headers["location"] == "/t/KO"
    path = observation_path(env / "journal", "KO")
    doc = json.loads(path.read_text())
    assert doc["price"] == 66.25 and doc["currency"] == "USD" and doc["note"] is None
    assert doc["source"] == "NYSE official close (broker statement)"
    assert _wait("KO").state == jobs.DONE and fake_build == [{"ticker": "KO", "fresh": False}]
    page = client.get("/t/KO").text
    assert "66.25" in page and 'action="/t/KO/price/remove"' in page


def test_a_price_recorded_during_a_run_gets_a_run_of_its_own(client, env, monkeypatch, fake_build):
    """The build reads the observation as it starts: a run already in
    flight when the price is recorded cannot carry it, so one more runs."""
    import threading
    gate, entered = threading.Event(), threading.Event()
    publish = reporting.build_report  # the fixture's publishing stand-in
    seen = []

    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        seen.append(observation_path(env / "journal", ticker).exists())
        if len(seen) == 1:
            entered.set()
            gate.wait(10)
        return publish(ticker, with_docs=with_docs, report_day=report_day, fresh=fresh, **kw)

    monkeypatch.setattr(reporting, "build_report", build)
    client.post("/t/KO/run", data={"fresh": "0"})
    assert entered.wait(10)
    assert client.post("/t/KO/price", data=_price_form()).status_code == 303
    first = jobs.latest("KO")
    gate.set()
    jobs.wait(first.id, timeout=30)
    second = _wait("KO")
    assert second.id != first.id and second.state == jobs.DONE
    assert seen == [False, True] and len(fake_build) == 2


def test_a_new_price_keeps_the_assumptions_and_scenarios_recorded_on_the_cli(client, env, fake_build):
    from app.services.valuation.observation import (
        Assumptions,
        MarketObservation,
        Scenario,
        write_observation,
    )

    now = datetime.now(UTC)
    write_observation(env / "journal", MarketObservation(
        ticker="KO", price=60.0, observed_at=now - timedelta(days=2), source="old",
        recorded_at=now - timedelta(days=2), assumptions=Assumptions(required_return=0.08),
        scenarios=(Scenario(name="bull", fcf_growth=0.1, years=5),)))
    page = client.get("/t/KO").text
    assert "1 scenario" in page and "Kept when you record a new price" in page
    assert client.post("/t/KO/price", data=_price_form()).status_code == 303
    doc = json.loads(observation_path(env / "journal", "KO").read_text())
    assert doc["price"] == 66.25 and doc["assumptions"]["required_return"] == 0.08
    assert [s["name"] for s in doc["scenarios"]] == ["bull"]
    _wait("KO")


@pytest.mark.parametrize("over,says", [
    ({"price": "nan"}, "price"),
    ({"price": "inf"}, "price"),
    ({"price": "-3"}, "price"),
    ({"price": "abc"}, "price"),
    ({"price": ""}, "price"),
    ({"observed_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}, "after recorded_at"),
    ({"observed_at": "2026-10-09T10:00"}, "observed_at"),
    ({"observed_at": "yesterday"}, "observed_at"),
    ({"source": "NYSE\nclose"}, "control characters"),
    ({"source": "NYSE close"}, "control characters"),
    ({"note": "a\x00b"}, "control characters"),
    ({"source": "   "}, "source must say what was looked at"),
    ({"currency": "DOLLARS"}, "currency"),
])
def test_an_invalid_price_says_why_and_writes_nothing(client, env, fake_build, over, says):
    before = _files(env)
    r = client.post("/t/KO/price", data=_price_form(**over))
    assert r.status_code == 400
    assert says in r.text and "Price not recorded" in r.text
    # What was typed is kept in the form (and not overwritten by app.js's "now").
    assert 'data-kept="1"' in r.text
    if "source" not in over:
        assert 'value="NYSE official close (broker statement)"' in r.text
    assert not observation_path(env / "journal", "KO").exists()
    assert _files(env) == before and fake_build == [] and len(jobs.REGISTRY) == 0


def test_remove_price_removes_it_and_rebuilds(client, env, fake_build):
    client.post("/t/KO/price", data=_price_form())
    _wait("KO")
    r = client.post("/t/KO/price/remove")
    assert r.status_code == 303 and r.headers["location"] == "/t/KO"
    assert not observation_path(env / "journal", "KO").exists()
    _wait("KO")
    assert len(fake_build) == 2
    r = client.post("/t/KO/price/remove")  # nothing left to remove
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "No+price+is+recorded" in r.headers["location"]


def test_a_price_file_in_the_way_is_shown_and_not_followed(client, env):
    market = env / "journal" / "market"
    market.mkdir(parents=True)
    target = env / "elsewhere.json"
    target.write_text("{}")
    os.symlink(target, market / "KO.json")
    page = client.get("/t/KO")
    assert page.status_code == 200 and "symlink" in page.text
    r = client.post("/t/KO/price", data=_price_form())
    assert r.status_code == 500 and "Price not recorded" in r.text and "symlink" in r.text
    assert target.read_text() == "{}"


# --- the watchlist --------------------------------------------------------------------


def _armable_submissions(today: date) -> dict:
    """Five quarters of 10-Qs, each with its earnings 8-K (Item 2.02) the
    same day, at a steady cadence ending a month ago: what `_arm` derives a
    row from (print hint from the 8-K cadence, event identity from the
    periodic filings)."""
    rows = []
    for i in range(5):
        filed = today - timedelta(days=30 + 91 * i)
        period = filed - timedelta(days=30)
        rows.append(("10-Q", f"0000021344-26-{i:06d}", filed.isoformat(), period.isoformat(), ""))
        rows.append(("8-K", f"0000021344-26-{100 + i:06d}", filed.isoformat(), filed.isoformat(),
                     "2.02,9.01"))
    return {"cik": "21344", "filings": {"recent": {
        "form": [r[0] for r in rows], "accessionNumber": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows], "reportDate": [r[3] for r in rows],
        "items": [r[4] for r in rows],
        "acceptanceDateTime": [f"{r[2]}T20:20:00.000Z" for r in rows],
        "primaryDocument": ["d.htm" for _ in rows]}}}


def test_watchlist_add_reuses_the_watch_scripts_arm_and_remove_drops_it(client, monkeypatch):
    fetched = []

    def submissions(ticker):
        fetched.append(ticker)
        return _armable_submissions(date.today())

    monkeypatch.setattr(watching, "_submissions", submissions)
    r = client.post("/watchlist", data={"ticker": "ko", "action": "add", "back": "ticker"})
    assert r.status_code == 303 and r.headers["location"].startswith("/t/KO")
    assert fetched == ["KO"]
    (w,) = wl.load()
    assert w.ticker == "KO" and w.event_armed and w.note  # the inference basis, as `add` writes
    page = client.get("/t/KO").text
    assert "Remove from watchlist" in page
    r = client.post("/watchlist", data={"ticker": "KO", "action": "remove", "back": "home"})
    assert r.status_code == 303 and r.headers["location"].startswith("/")
    assert wl.load() == []
    assert "Add to watchlist" in client.get("/t/KO").text


def test_watchlist_add_when_sec_fails_is_a_message_not_a_500(client, monkeypatch):
    def submissions(ticker):
        raise sec_client.SecClientError("HTTP 503 from data.sec.gov")

    monkeypatch.setattr(watching, "_submissions", submissions)
    r = client.post("/watchlist", data={"ticker": "KO", "action": "add", "back": "ticker"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "SEC fetch failed: HTTP 503 from data.sec.gov" in page.text
    assert wl.load() == []


def test_watchlist_add_twice_and_remove_unknown_are_messages(client, monkeypatch):
    monkeypatch.setattr(watching, "_submissions", lambda t: _armable_submissions(date.today()))
    client.post("/watchlist", data={"ticker": "KO", "action": "add"})
    r = client.post("/watchlist", data={"ticker": "KO", "action": "add"})
    assert "already+on+the+watchlist" in r.headers["location"]
    r = client.post("/watchlist", data={"ticker": "CRM", "action": "remove"})
    assert "error=" in r.headers["location"]


def test_a_watch_with_a_pinned_thesis_is_not_removed_from_the_page(client):
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00",
                  "thesis_entry": "2026-10-01", "thesis_sha256": "ab" * 32})
    r = client.post("/watchlist", data={"ticker": "KO", "action": "remove"})
    assert r.status_code == 303 and "pinned+thesis" in r.headers["location"]
    assert [w.ticker for w in wl.load()] == ["KO"]


@pytest.mark.parametrize("data", [
    {"ticker": "KO", "action": "drop"},
    {"ticker": "../x", "action": "add"},
    {"ticker": "KO", "action": "add", "back": "https://evil.example/"},
])
def test_watchlist_bad_input_is_refused_and_never_redirects_offsite(client, monkeypatch, data):
    monkeypatch.setattr(watching, "_submissions", lambda t: pytest.fail("must not fetch"))
    if data.get("back"):
        monkeypatch.setattr(watching, "_submissions", lambda t: _armable_submissions(date.today()))
    r = client.post("/watchlist", data=data)
    assert r.status_code in (303, 400)
    if r.status_code == 303:
        assert r.headers["location"].startswith("/") and not r.headers["location"].startswith("//")


# --- the guard on every new route -----------------------------------------------------


NEW_POSTS = [
    ("/t", {"ticker": "KO"}),
    ("/t/KO/run", {"fresh": "0"}),
    ("/t/KO/price", None),
    ("/t/KO/price/remove", {}),
    ("/watchlist", {"ticker": "KO", "action": "add"}),
]


@pytest.mark.parametrize("path,data", NEW_POSTS)
def test_every_new_post_is_refused_from_another_site(client, env, path, data, fake_build):
    before = _files(env)
    data = _price_form() if data is None else data
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        r = client.post(path, data=data, headers=headers)
        assert r.status_code == 403 and "Refused" in r.text
    assert _files(env) == before and fake_build == [] and len(jobs.REGISTRY) == 0


@pytest.mark.parametrize("path,data", NEW_POSTS)
def test_every_new_post_is_refused_to_another_machine(env, path, data, fake_build):
    remote = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.9", 50000),
                        follow_redirects=False)
    data = _price_form() if data is None else data
    assert remote.post(path, data=data).status_code == 403
    assert fake_build == [] and len(jobs.REGISTRY) == 0


@pytest.mark.parametrize("path", ["/", "/journal", "/t/KO", "/t/KO/status", "/static/app.css",
                                  "/static/app.js"])
def test_every_new_page_is_guarded(env, path):
    remote = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.9", 50000))
    assert remote.get(path).status_code == 403
    rebound = TestClient(app, base_url="http://evil.example", client=("127.0.0.1", 50000))
    assert rebound.get(path).status_code == 400


def test_static_files_are_served(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert "prefers-color-scheme" in css.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "data-job-status" in js.text
    assert client.get("/static/../web.py").status_code == 404


def test_no_page_loads_anything_from_another_origin(client, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    _wait("KO")
    for path in ("/", "/t/KO", "/journal", "/review"):
        text = client.get(path).text
        assert not re.search(r'<(?:script|link)[^>]+(?:src|href)="(?:https?:)?//', text), path


# --- the real build, end to end -------------------------------------------------------


def test_the_real_offline_build_end_to_end_with_a_price(client, sec, env):
    """KO, offline, through the routes as a browser drives them: type the
    ticker, run, read the card; record a price, and the next run's
    appendix carries the valuation shadow card (and its ledger says so)."""
    r = client.post("/t", data={"ticker": "ko"})
    assert r.headers["location"] == "/t/KO"
    assert "No report yet" in client.get("/t/KO").text
    client.post("/t/KO/run", data={"fresh": "0"})
    first = _wait("KO")
    assert first.state == jobs.DONE, first.error
    page = client.get("/t/KO").text
    assert "Decision Card — KO" in page and "Attention flags" in page
    assert SECTION_TITLE not in page.split("<details", 1)[0]
    # No observation: the price box says so, and links to no section.
    assert "In the live run: not produced" in page and 'href="#valuation"' not in page
    live = read_live(_workbench("KO"))
    assert live is not None and live.generation_id == first.generation_id
    assert not reporting.report_path("KO").exists()  # never at the journal's live name

    r = client.post("/t/KO/price", data=_price_form())
    assert r.status_code == 303, r.text
    second = _wait("KO")
    assert second.state == jobs.DONE, second.error
    assert second.generation_id != first.generation_id
    page = client.get("/t/KO").text
    card, appendix = page.split("<details", 1)
    assert SECTION_TITLE in appendix and SECTION_TITLE not in card
    # The price box links to it, and the heading carries the id it names.
    assert 'href="#valuation"' in appendix and f'<h2 id="valuation">{SECTION_TITLE}</h2>' in appendix
    doc = LedgerDocument.model_validate_json(ledger_path(_workbench("KO")).read_text())
    assert doc.valuation is not None and doc.valuation.state == "produced"
    history = page.split('id="history"', 1)[1]
    assert len(re.findall(r'class="run-row', history)) == 2
    old = client.get(f"/t/KO/runs/{first.generation_id}")
    assert old.status_code == 200 and SECTION_TITLE not in old.text

    # "Refresh from SEC" bypasses the cache: with SEC down it fails, said
    # in one line, and the live run stays the one it was.
    client.post("/t/KO/run", data={"fresh": "1"})
    third = _wait("KO")
    assert third.state == jobs.FAILED and third.error.startswith("SEC fetch failed")
    assert read_live(_workbench("KO")).generation_id == second.generation_id
    assert "SEC fetch failed" in client.get("/t/KO").text


# --- fix round 1 (independent review of 9d00328) ---------------------------------------


def _held_build(monkeypatch, publish):
    """`build_report` whose first call waits for ``gate``; then publishes."""
    import threading

    gate, entered = threading.Event(), threading.Event()
    seen: list[bool] = []

    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        seen.append(fresh)
        if len(seen) == 1:
            entered.set()
            gate.wait(10)
        return publish(ticker, with_docs=with_docs, report_day=report_day, fresh=fresh, **kw)

    monkeypatch.setattr(reporting, "build_report", build)
    return gate, entered, seen


def test_refresh_during_a_cached_run_is_queued_and_said(client, monkeypatch, fake_build):
    """Reviewer's jobsrepro.py #2, through the page: the status said the
    cached run and the refresh was dropped. It is queued, and said."""
    gate, entered, seen = _held_build(monkeypatch, reporting.build_report)
    client.post("/t/KO/run", data={"fresh": "0"})
    assert entered.wait(10)
    frag = client.post("/t/KO/run", data={"fresh": "1"}, headers={"HX-Request": "true"})
    assert frag.status_code == 200 and 'data-state="running"' in frag.text
    assert "a refresh from SEC follows" in frag.text
    first = jobs.latest("KO")
    gate.set()
    jobs.wait(first.id, timeout=30)
    second = _wait("KO")
    assert second.id != first.id and second.fresh and seen == [False, True]
    assert "Refreshing from SEC" not in client.get("/t/KO/status").text


def test_a_fresh_run_in_flight_is_said_as_refreshing(client, monkeypatch, fake_build):
    gate, entered, _ = _held_build(monkeypatch, reporting.build_report)
    client.post("/t/KO/run", data={"fresh": "1"})
    assert entered.wait(10)
    frag = client.get("/t/KO/status").text
    assert "Refreshing from SEC" in frag and "follows" not in frag
    gate.set()
    _wait("KO")


def test_a_stalled_run_is_said_and_run_starts_another(client, monkeypatch, fake_build):
    gate, entered, seen = _held_build(monkeypatch, reporting.build_report)
    client.post("/t/KO/run", data={"fresh": "0"})
    assert entered.wait(10)
    first = jobs.latest("KO")
    later = (first.started_at or first.created_at) + timedelta(seconds=jobs.STALL_AFTER_S + 1)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    frag = client.get("/t/KO/status").text
    assert 'data-state="stalled"' in frag and "stalled" in frag.lower()
    assert "Run it again" in frag
    page = client.get("/t/KO").text
    assert re.search(r'<form[^>]*action="/t/KO/run"', page)
    assert "Running now" not in client.get("/").text   # not a run in flight any more
    client.post("/t/KO/run", data={"fresh": "0"})
    second = jobs.latest("KO")
    assert second.id != first.id
    second = jobs.wait(second.id, timeout=30)
    gate.set()
    jobs.wait(first.id, timeout=30)
    # The abandoned run ended after the run that replaced it published: it
    # is superseded, never live (Hermes audit of PR #118, finding 1).
    assert jobs.REGISTRY.get(first.id).state == jobs.SUPERSEDED
    assert read_live(_workbench("KO")).generation_id == second.generation_id
    assert seen == [False, False]


def test_a_failed_runs_banner_goes_once_a_newer_run_is_published(client, monkeypatch, env):
    """The banner said the last run failed for as long as the server held
    that run, even with a newer run published since (another workbench
    process; an abandoned run that finished after all)."""
    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        raise sec_client.SecClientError("HTTP 503")

    monkeypatch.setattr(reporting, "build_report", build)
    older = _workbench("KO")
    older.parent.mkdir(parents=True)
    with replacing(older, now=datetime.now(UTC) - timedelta(hours=2)) as staged:
        staged.report.write_text(CARD.format(t="KO", day=date.today(), n=0))
        staged.ledger.write_text("{}")
    client.post("/t/KO/run", data={"fresh": "1"})
    failed = _wait("KO")
    assert failed.state == jobs.FAILED
    # A run published before the failed one started: the failure stands.
    assert "The last run failed" in client.get("/t/KO/status").text
    newer = _workbench("KO", "2026-01-02")  # any day of the ticker counts
    # Stamped a second past the failure (a stamp in its own second is not
    # counted: it may be from before it).
    with replacing(newer, now=failed.finished_at + timedelta(seconds=1)) as staged:
        staged.report.write_text(CARD.format(t="KO", day=date.today(), n=9))
        staged.ledger.write_text("{}")
    frag = client.get("/t/KO/status").text
    assert "The last run failed" not in frag and 'data-state="failed"' in frag
    assert "The last run failed" not in client.get("/t/KO").text


def test_the_poller_says_a_restarted_server_instead_of_stopping_silently(client):
    """A run's status read back as "idle" (the server restarted and forgot
    it) used to stop the poller with the spinner gone and nothing said."""
    js = client.get("/static/app.js").text
    assert 'now === "idle"' in js and "server restarted" in js and "reload" in js.lower()
    assert '"stalled"' in js


@pytest.mark.parametrize("typed", ["61.20", "0", "1700000000", "-1", "1e9"])
def test_an_epoch_is_not_a_time_the_form_accepts(client, env, fake_build, typed):
    """Reviewer's pricerepro.py: the model read "1700000000" (and "0", and
    the price typed into the wrong box) as Unix seconds and recorded
    1970-01-01 or 2023-11-14. The form's time is parsed by the function
    `market.py record --at` uses: an ISO-8601 time with its offset."""
    before = _files(env)
    r = client.post("/t/KO/price", data=_price_form(observed_at=typed))
    assert r.status_code == 400 and "Price not recorded" in r.text
    assert f"observed_at {typed!r}" in r.text.replace("&#39;", "'")
    assert _files(env) == before and fake_build == []


# (Left out, the currency is the form's own, USD, as an omitted --currency is
# the CLI's. The box records USD only: test_the_price_box_records_usd_only.)
@pytest.mark.parametrize("currency", ["usd", " EUR ", "Usd", "US$", "EURO"])
def test_the_currency_is_refused_as_the_cli_refuses_it(client, env, fake_build, currency):
    r = client.post("/t/KO/price", data=_price_form(currency=currency))
    assert r.status_code == 400 and "currency" in r.text
    assert not observation_path(env / "journal", "KO").exists()


def _cli_record(env, monkeypatch, form: dict) -> int:
    import contextlib
    import io

    from scripts import market

    monkeypatch.setattr(market, "_journal", lambda: env / "journal")
    argv = ["record", "KO", "--price", form["price"], "--at", form["observed_at"],
            "--source", form["source"], "--currency", form["currency"]]
    if form.get("note"):
        argv += ["--note", form["note"]]
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return market.main(argv)


@pytest.mark.parametrize("over", [
    {}, {"observed_at": "0"}, {"observed_at": "1700000000"}, {"observed_at": "61.20"},
    {"observed_at": "2026-10-01T10:00"}, {"observed_at": "2026-10-01"},
    {"observed_at": "2026-10-01T10:00:00Z"}, {"observed_at": "2026-10-01T10:00-04:00"},
    {"observed_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
    # Not {"currency": "EUR"}: the CLI records it, the box records USD only
    # (Hermes audit of PR #118, finding 2; test_the_price_box_records_usd_only).
    {"currency": "usd"}, {"currency": " EUR "}, {"currency": "US$"},
    {"source": "a b"}, {"note": "a\u0085b"}, {"source": "x" * 201}, {"price": "nan"},
])
def test_the_form_and_the_cli_accept_and_refuse_the_same(client, env, fake_build, monkeypatch,
                                                         over):
    form = _price_form(**over)
    path = observation_path(env / "journal", "KO")
    r = client.post("/t/KO/price", data=form)
    web = path.exists()
    stored = json.loads(path.read_text())["observed_at"] if web else None
    path.unlink(missing_ok=True)
    rc = _cli_record(env, monkeypatch, form)
    cli = json.loads(path.read_text())["observed_at"] if rc == 0 else None
    assert (r.status_code == 303) == web == (rc == 0), (r.status_code, rc)
    assert stored == cli
    if web:
        _wait("KO")


def test_watchlist_add_with_an_odd_sec_payload_is_a_message(client, monkeypatch):
    """Reviewer's watchrepro.py: `_arm` raises `PollerError` on a payload
    of an unexpected shape; it was a 500."""
    for payload in ({"filings": {"recent": {"form": ["8-K"], "items": None}}}, []):
        monkeypatch.setattr(watching, "_submissions", lambda t, p=payload: p)
        r = client.post("/watchlist", data={"ticker": "PEP", "action": "add"})
        assert r.status_code == 303 and "error=" in r.headers["location"]
        page = client.get(r.headers["location"]).text
        assert "filing index" in page and "unexpected submissions payload shape" in page
    assert wl.load() == []


def test_watchlist_changes_with_the_lock_in_the_way_are_a_message(client, env, monkeypatch):
    """A link at the watchlist's lock (O_NOFOLLOW refuses it, ELOOP): a
    remove, and an add, were 500s."""
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    lock = wl.WATCHLIST.with_name(wl.WATCHLIST.name + ".lock")
    lock.unlink(missing_ok=True)
    lock.symlink_to(env / "elsewhere")
    r = client.post("/watchlist", data={"ticker": "KO", "action": "remove", "back": "ticker"})
    assert r.status_code == 303 and r.headers["location"].startswith("/t/KO?error=")
    assert "watchlist+could+not+be+changed" in r.headers["location"]
    monkeypatch.setattr(watching, "_submissions", lambda t: _armable_submissions(date.today()))
    r = client.post("/watchlist", data={"ticker": "PEP", "action": "add"})
    assert r.status_code == 303 and "watchlist+could+not+be+changed" in r.headers["location"]
    assert [w.ticker for w in wl.load()] == ["KO"] and not (env / "elsewhere").exists()


def test_a_page_view_creates_no_file(client, env, monkeypatch):
    """Reviewer's getside.py: the setup check created (and removed) a probe
    file in the reports directory on every GET of the home page."""
    import tempfile

    def no_files(*a, **k):
        raise AssertionError("a GET created a file")

    monkeypatch.setattr(tempfile, "mkstemp", no_files)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_files)
    (env / "reports").mkdir()
    before = _files(env)
    for path in ("/", "/t/KO", "/t/KO/status", "/journal"):
        assert client.get(path).status_code == 200, path
    assert _files(env) == before


def test_a_symlink_inside_a_generation_is_a_problem_line(client, env, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    done = _wait("KO")
    live = read_live(_workbench("KO"))
    gen = live.report.parent
    os.chmod(gen, 0o755)
    outside = env / "outside.md"
    outside.write_text(f"# Decision Card — KO\nsecret\n- Generation: {done.generation_id}\n")
    live.report.unlink()
    live.report.symlink_to(outside)
    r = client.get("/t/KO")
    assert r.status_code == 200 and "secret" not in r.text
    assert "cannot be read" in r.text and "symlink" in r.text
    assert client.get(f"/t/KO/runs/{done.generation_id}").status_code == 500


def test_a_real_build_waits_for_the_publish_lock_only_so_long(client, sec, env, monkeypatch):
    """The real build, offline: another publisher holds the workbench
    report's lock past `review.PUBLISH_WAIT_S`, and the run fails as busy
    (rather than waiting for as long as the lock is held), publishing
    nothing."""
    import threading

    from app.services.journal import review
    from app.services.reporting.report_files import publish_lock

    monkeypatch.setattr(review, "PUBLISH_WAIT_S", 0.5)
    report = _workbench("KO")
    report.parent.mkdir(parents=True)
    held, release = threading.Event(), threading.Event()

    def hold():
        with publish_lock(report):
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        client.post("/t/KO/run", data={"fresh": "0"})
        job = jobs.latest("KO")
        done = jobs.wait(job.id, timeout=45)
    finally:
        release.set()
        holder.join(10)
    assert done.state == jobs.FAILED, done
    assert done.error == "another run is publishing this report; try again"
    assert read_live(report) is None and not reporting.report_path("KO").exists()
    assert "another run is publishing" in client.get("/t/KO/status").text


# --- fix round 2 (Hermes audit of PR #118 @ 3983f8a) -----------------------------------


class _Ids(HTMLParser):
    """Every ``id`` attribute of a page, and every label's ``for``."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.label_for: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") is not None:
            self.ids.append(a["id"])
        if tag == "label" and a.get("for"):
            self.label_for.append(a["for"])

    handle_startendtag = handle_starttag


def _ids(html: str) -> _Ids:
    parser = _Ids()
    parser.feed(html)
    return parser


def _duplicates(html: str) -> list[str]:
    seen = _ids(html).ids
    return sorted({i for i in seen if seen.count(i) > 1})


def test_every_page_has_unique_ids(client, env, fake_build, monkeypatch):
    """Finding 3: the price panel and the price input were both
    ``id="price"`` on the ticker page (a label, the ``#price`` anchor and
    the browser's form handling each took the first). Every page, in each
    of its states."""
    pages = {}
    monkeypatch.delenv("EDGAR_IDENTITY")
    pages["home, setup state"] = client.get("/")
    monkeypatch.setenv("EDGAR_IDENTITY", IDENTITY)
    pages["home"] = client.get("/")
    pages["ticker, no run"] = client.get("/t/KO")
    pages["status, idle"] = client.get("/t/KO/status")
    client.post("/t/KO/price", data=_price_form(note="after the call"))
    job = _wait("KO")
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    pages["ticker, with a run and a price"] = client.get("/t/KO")
    pages["status, done"] = client.get("/t/KO/status")
    pages["past run"] = client.get(f"/t/KO/runs/{job.generation_id}")
    pages["home, with runs"] = client.get("/")
    pages["price refused"] = client.post("/t/KO/price", data=_price_form(price="-1"))
    store.open_entry("PEP", "a thesis", 3, "hold")
    pages["journal"] = client.get("/journal")
    pages["open"] = client.get("/open")
    pages["impact"] = client.get("/impact/PEP")
    pages["report"] = client.get("/report/PEP")
    pages["review board"] = client.get("/review")
    for name, r in pages.items():
        assert r.status_code in (200, 400), (name, r.status_code)
        assert _duplicates(r.text) == [], name
    page = pages["ticker, with a run and a price"].text
    assert 'id="price-panel"' in page and 'id="price"' in page
    assert re.search(r'<input id="price" name="price"', page)


def test_every_response_refuses_to_be_framed(client, fake_build, monkeypatch):
    """Finding 4: no page said it may not be framed, so another site could
    load the workbench in a frame and steer a click onto "Record price" or
    "Remove from watchlist" (clickjacking): the Origin check passes a click
    made on the page itself. Every response says so, the guard's own
    refusals and errors included."""
    responses = {
        "home": client.get("/"),
        "ticker": client.get("/t/KO"),
        "status": client.get("/t/KO/status"),
        "journal": client.get("/journal"),
        "review": client.get("/review"),
        "static css": client.get("/static/app.css"),
        "static js": client.get("/static/app.js"),
        "404": client.get("/no/such/page"),
        "redirect": client.post("/t", data={"ticker": "ko"}),
        "400 bad ticker": client.post("/t", data={"ticker": "../x"}),
        "403 foreign origin": client.post("/t/KO/run", data={"fresh": "0"},
                                          headers={"origin": "https://evil.example"}),
        "400 foreign host": client.get("/", headers={"host": "evil.example"}),
        "403 another machine": TestClient(app, base_url="http://127.0.0.1",
                                          client=("192.168.1.20", 50000)).get("/"),
    }
    monkeypatch.setenv("FQE_WEB_ALLOWED_HOSTS", "*")
    responses["500 misconfigured"] = client.get("/")
    monkeypatch.delenv("FQE_WEB_ALLOWED_HOSTS")
    statuses = {name: r.status_code for name, r in responses.items()}
    assert statuses["404"] == 404 and statuses["redirect"] == 303
    assert statuses["403 foreign origin"] == 403 and statuses["400 foreign host"] == 400
    assert statuses["403 another machine"] == 403 and statuses["500 misconfigured"] == 500
    for name, r in responses.items():
        assert r.headers.get("x-frame-options") == "DENY", name
        assert r.headers.get("content-security-policy") == "frame-ancestors 'none'", name
    assert fake_build == []


def test_the_price_box_records_usd_only(client, env, fake_build):
    """Finding 2: the box took any three capitals, and the bridge multiplied
    a EUR price by the share count and added USD debt to it. The filing
    figures are USD (`fields.FILING_CURRENCY`); the box says so and is not
    a text box, and a posted currency other than it is refused."""
    page = client.get("/t/KO").text
    box = re.search(r'<input[^>]*name="currency"[^>]*>', page)
    assert box is not None and 'type="hidden"' in box.group(0) and 'value="USD"' in box.group(0)
    assert '<input id="currency"' not in page and "USD" in page.split('id="price-panel"')[1]
    for currency in ("EUR", "GBP", "JPY"):
        r = client.post("/t/KO/price", data=_price_form(currency=currency))
        assert r.status_code == 400 and "Price not recorded" in r.text
        assert "USD only" in r.text and "no FX conversion" in r.text
        assert not observation_path(env / "journal", "KO").exists()
    assert fake_build == [] and len(jobs.REGISTRY) == 0
    # Left out, it is USD: the box's own value.
    form = _price_form()
    del form["currency"]
    assert client.post("/t/KO/price", data=form).status_code == 303
    assert json.loads(observation_path(env / "journal", "KO").read_text())["currency"] == "USD"
    _wait("KO")


def test_a_eur_price_recorded_on_the_cli_shows_the_refusal_on_the_card(client, sec, env):
    """The CLI still records a well-formed currency; the plane then refuses
    it as the box would: the run's appendix and ledger say no EV, and the
    card above it is the run without a price's."""
    from app.services.valuation.observation import MarketObservation, write_observation

    client.post("/t/KO/run", data={"fresh": "0"})
    plain = _wait("KO")
    assert plain.state == jobs.DONE, plain.error
    card = read_live(_workbench("KO")).text.split("# Full report (appendix)")[0]
    now = datetime.now(UTC)
    write_observation(env / "journal", MarketObservation(
        ticker="KO", price=61.0, currency="EUR", observed_at=now - timedelta(hours=1),
        source="Xetra close", recorded_at=now))
    client.post("/t/KO/run", data={"fresh": "0"})
    run = _wait("KO")
    assert run.state == jobs.DONE, run.error
    live = read_live(_workbench("KO"))
    assert live.generation_id == run.generation_id
    assert live.text.split("# Full report (appendix)")[0].replace(
        plain.generation_id, run.generation_id) == card.replace(
        plain.generation_id, run.generation_id)
    reason = "price in EUR, filing figures in USD — no FX conversion"
    assert f"EV not asserted: {reason}" in live.text
    doc = LedgerDocument.model_validate_json(live.ledger.read_text())
    assert doc.valuation.ev is None and reason in doc.valuation.ev_reason
    page = client.get("/t/KO").text
    assert "61.00 EUR" in page and reason in page
    panel = page.split('id="price-panel"', 1)[1].split("</section>", 1)[0]
    assert "This price is in EUR and the filing figures are in USD" in panel
    assert "This price is in" not in client.get("/t/CRM").text


def test_the_real_build_seals_each_runs_request_number(client, sec, env):
    """Finding 1, through the real build: each run asked for is given the
    next number of the ticker's counter and its ledger carries it."""
    fences = []
    for _ in range(2):
        client.post("/t/KO/run", data={"fresh": "0"})
        job = _wait("KO")
        assert job.state == jobs.DONE, job.error
        fences.append(LedgerDocument.model_validate_json(
            read_live(_workbench("KO")).ledger.read_text()).fence)
    assert fences == [1, 2]


def test_a_refused_price_keeps_everything_typed(client, env, fake_build):
    form = _price_form(price="-3", source="my broker statement & co", note="after the call",
                       observed_at="2026-10-08T16:00-04:00")
    r = client.post("/t/KO/price", data=form)
    assert r.status_code == 400 and "Price not recorded" in r.text
    for name in ("price", "observed_at", "source", "note"):
        typed = html_escape(form[name])
        assert re.search(rf'<input id="{name}" name="{name}"[^>]*value="{re.escape(typed)}"',
                         r.text), name
    assert 'data-kept="1"' in r.text


def test_the_poller_says_when_it_loses_the_server_and_can_retry(client):
    """Finding 6: a status poll that failed (the server stopped, a 500) was
    retried silently, the spinner turning forever; it is said on the page,
    with a way to try again and a way to reload."""
    js = client.get("/static/app.js").text
    assert "Lost contact with the local server" in js
    assert "data-poll-retry" in js and "data-poll-reload" in js
    assert "Retry" in js and "Reload" in js
    # A failed poll says so (and stops), never only retries in silence; the
    # behaviour itself was exercised in headless Chromium (r36 fix round 2).
    assert re.search(r"\.catch\(function \(e\) \{ lost\(", js)
    assert "POLL_MS * 3" not in js
    assert re.search(r"retry\.addEventListener\(\"click\", function \(\) \{ said\.remove\(\); poll\(\); \}\)", js)
    css = client.get("/static/app.css").text
    assert ".poll-lost" in css


def test_the_status_box_names_the_live_run_after_a_done_run(client, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    job = _wait("KO")
    frag = client.get("/t/KO/status").text
    assert job.generation_id in frag and "is the live run" in frag


# --- fix round 3 (independent review of 2cbba1c) ---------------------------------------


def test_a_run_asked_for_before_midnight_never_becomes_the_card_after_it(client, sec, env,
                                                                          monkeypatch):
    """H1, through the real offline build: run A hangs on its SEC read, B
    is asked for and publishes on day 1, midnight passes, A ends and names
    its report for day 2 (the build dates its file after the fetch), where
    nothing is live. The ticker's high-water mark refuses it there too."""
    import threading

    from app.services.workbench import views

    d1, d2 = date(2026, 10, 9), date(2026, 10, 10)
    today = [d1]

    class Day(date):
        @classmethod
        def today(cls):
            return today[0]

    monkeypatch.setattr(reporting, "date", Day)
    gate, entered, calls = threading.Event(), threading.Event(), []
    real_fetch = reporting.fetch_dataset_snapshot

    def fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:  # A: a hung SEC read
            entered.set()
            gate.wait(60)
        return real_fetch(*a, **k)

    monkeypatch.setattr(reporting, "fetch_dataset_snapshot", fetch)
    now = [datetime(2026, 10, 9, 23, 40, tzinfo=UTC)]
    monkeypatch.setattr(jobs, "_now", lambda: now[0])
    a = jobs.start("KO")
    assert entered.wait(30)
    now[0] += timedelta(seconds=jobs.STALL_AFTER_S + 1)
    b = jobs.wait(jobs.start("KO").id, timeout=120)
    assert b.state == jobs.DONE, b.error
    today[0] = d2
    gate.set()
    a = jobs.wait(a.id, timeout=120)
    assert a.state == jobs.SUPERSEDED, a
    assert read_live(_workbench("KO", d2.isoformat())) is None
    assert read_live(_workbench("KO", d1.isoformat())).generation_id == b.generation_id
    assert views.live_generation("KO") == b.generation_id
    assert views.ticker_view("KO").latest.generation_id == b.generation_id
    page = client.get("/t/KO").text
    assert f"generation <code>{b.generation_id}</code>" in page
    history = page.split('id="history"', 1)[1]
    assert "superseded" in history  # A is kept, said as such
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    rows, _ = views.watchlist_rows()
    assert [r.latest.generation_id for r in rows] == [b.generation_id]
    assert [r.ref.generation_id for r in views.recent_runs()] == [b.generation_id]


def test_an_unhandled_error_page_refuses_to_be_framed_too(client, monkeypatch):
    """N1: a 500 from an unhandled exception is answered outside `_guard`
    (Starlette's outermost error middleware): it carries the two headers
    from the app's own handler."""
    from app.services.workbench import views

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(views, "ticker_view", boom)
    c = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                   raise_server_exceptions=False)
    r = c.get("/t/KO")
    assert r.status_code == 500 and "boom" not in r.text
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("content-security-policy") == "frame-ancestors 'none'"


# --- fix round 4 (Hermes re-audit of #118 @ 34836cf): polish ----------------------------


def test_a_refused_price_is_announced_and_tied_to_its_fields(client, env, fake_build):
    r = client.post("/t/KO/price", data=_price_form(price="-3"))
    assert r.status_code == 400
    m = re.search(r'<div class="err" role="alert" id="([a-z-]+)">Price not recorded', r.text)
    assert m, "the refusal is not announced"
    for name in ("price", "observed_at", "source", "note"):
        tag = re.search(rf'<input id="{name}"[^>]*>', r.text).group(0)
        assert f'aria-describedby="{m.group(1)}"' in tag, name
    # No refusal: no field points at an error that is not there.
    page = client.get("/t/KO").text
    assert 'aria-describedby="price-error"' not in page


def test_workbench_pages_have_their_own_titles(client, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    job = _wait("KO")
    for url, want in (("/", "Workbench"), ("/t/KO", "KO"),
                      (f"/t/KO/runs/{job.generation_id}", "KO"),
                      ("/t/KO/runs/" + "0" * 32, "No such run")):
        title = re.search(r"<title>(.*?)</title>", client.get(url).text).group(1)
        assert want in title and "FQE Workbench" in title, (url, title)


# --- fix round 5 (independent review of 6bf9f9e) ---------------------------------------


def test_the_report_day_is_the_requests_not_the_one_after_the_fetch(client, sec, env,
                                                                    monkeypatch):
    """M1, through the real offline build: the run is asked for on day 1 and
    its fetch ends on day 2; its report is day 1's, the day it was numbered
    on, so day order follows request order."""
    from app.services.workbench import fencing

    d1, d2 = date(2026, 10, 9), date(2026, 10, 10)

    class Asked(date):
        @classmethod
        def today(cls):
            return d1

    class Built(date):
        @classmethod
        def today(cls):
            return d2

    monkeypatch.setattr(fencing, "date", Asked)
    monkeypatch.setattr(reporting, "date", Built)
    client.post("/t/KO/run", data={"fresh": "0"})
    job = _wait("KO")
    assert job.state == jobs.DONE, job.error
    assert job.day == d1.isoformat()
    assert read_live(_workbench("KO", d1.isoformat())).generation_id == job.generation_id
    assert read_live(_workbench("KO", d2.isoformat())) is None
    # The card still says when it was generated.
    assert "As of 2026-10-10" in read_live(_workbench("KO", d1.isoformat())).text


def test_review_and_past_run_pages_answer_head(client, fake_build):
    client.post("/t/KO/run", data={"fresh": "0"})
    job = _wait("KO")
    assert client.head(f"/t/KO/runs/{job.generation_id}").status_code == 200
    assert client.head("/review").status_code == 200


def test_no_alert_is_nested_in_a_live_region(client, fake_build, monkeypatch):
    """A `role="alert"` inside `aria-live` is announced twice (or not at
    all) depending on the reader: the status box is the live region."""
    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        raise sec_client.SecClientError("HTTP 503")

    monkeypatch.setattr(reporting, "build_report", build)
    client.post("/t/KO/run", data={"fresh": "0"})
    _wait("KO")
    frag = client.get("/t/KO/status").text
    assert 'aria-live="polite"' in frag and "The last run failed" in frag
    assert 'role="alert"' not in frag
    js = client.get("/static/app.js").text
    assert 'setAttribute("role", "alert")' not in js
