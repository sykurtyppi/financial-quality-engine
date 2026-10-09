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
    through `replacing`, as the real build publishes. Returns the calls."""
    calls: list[dict] = []

    def build(ticker, with_docs=True, report_day=None, fresh=False, **kw):
        calls.append({"ticker": ticker, "fresh": fresh})
        out = reporting.report_path(ticker, report_day)
        out.parent.mkdir(parents=True, exist_ok=True)
        with replacing(out) as staged:
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
    assert r.status_code == 200 and "setup" not in r.text.split("<main>", 1)[1]


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
    day = date.today().isoformat()
    assert f'href="/review/KO?date={day}"' in r.text
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
    r = client.post("/t/KO/price", data=_price_form(currency="usd"))
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
        return publish(ticker, with_docs=with_docs, report_day=report_day, fresh=fresh)

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
    live = read_live(reporting.report_path("KO"))
    assert live is not None and live.generation_id == first.generation_id

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
    doc = LedgerDocument.model_validate_json(ledger_path(reporting.report_path("KO")).read_text())
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
    assert read_live(reporting.report_path("KO")).generation_id == second.generation_id
    assert "SEC fetch failed" in client.get("/t/KO").text
