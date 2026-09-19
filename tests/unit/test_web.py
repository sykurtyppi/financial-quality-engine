"""Local journal web UI — offline route tests (report generation stubbed).

Verifies the four screens operate on the shared store: dashboard renders, opening
a case writes an entry, the report route locks the thesis and renders, and the
impact form writes the AFTER/OUTCOME fields back to the same markdown file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.journal import reporting, store
from app.web import app


def _seed(ticker: str, thesis: str = "a thesis", conviction: int = 3, action: str = "hold"):
    """A legacy (v1) case, written straight to the store. The web form that
    used to create these is retired — only the format's READ paths remain — so
    tests seed them the way the v1 CLI still does."""
    return store.open_entry(ticker, thesis, conviction, action)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "entries")
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")

    def fake_build(ticker, with_docs=True, report_day=None, fresh=False):
        p = reporting.report_path(ticker, report_day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {ticker} report\n\n| Block | Score |\n|---|---|\n| Earnings Quality | 29 |\n")
        return p, 31.2

    monkeypatch.setattr(reporting, "build_report", fake_build)
    return TestClient(app, follow_redirects=False)


def test_dashboard_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text and "No cases yet" in r.text


def test_the_web_no_longer_opens_cases(client):
    # A v1 entry has no hash-locked BEFORE block, so it can never be
    # preregistered evidence. The form is retired, not repaired — and the
    # refusal is at the route, so a stale bookmark or forged POST is refused
    # too, whatever it sends.
    r = client.post("/open", data={"ticker": "nvda", "thesis": "beat priced in",
                                   "conviction": 3, "action": "hold"})
    assert r.status_code == 303 and "/open?error=" in r.headers["location"]
    assert store.find_entry("NVDA") is None
    assert client.post("/open", data={}).status_code == 303          # no fields at all
    assert client.post("/open", data={"ticker": "../../../pwned"}).status_code == 303
    assert store.list_entries() == []


def test_open_page_points_at_the_cli(client):
    r = client.get("/open")
    assert r.status_code == 200
    assert "openv2" in r.text and "retired" in r.text
    assert 'action="/open"' not in r.text        # the form itself is gone


def test_report_locks_and_renders(client):
    _seed("AAPL", "clean compounder", 4, "hold")
    r = client.get("/report/AAPL")
    assert r.status_code == 200
    assert "AAPL report" in r.text and "<table>" in r.text          # markdown rendered to HTML
    entry = store.parse_entry(store.find_entry("AAPL"))
    assert entry["is_reported"]                                     # thesis locked on first view


def test_report_requires_thesis(client):
    # open with a placeholder thesis via the store directly, then hit report
    store.open_entry("XYZ")  # placeholder thesis
    r = client.get("/report/XYZ")
    assert r.status_code == 303 and "/open" in r.headers["location"]


def test_first_report_is_generated_fresh(client, monkeypatch):
    seen = {}

    def build(ticker, with_docs=True, report_day=None, fresh=False):
        seen["fresh"] = fresh
        p = reporting.report_path(ticker, report_day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# r")
        return p, "ok"

    monkeypatch.setattr(reporting, "build_report", build)
    _seed("KO", "steady staple", 3, "hold")
    assert client.get("/report/KO").status_code == 200
    assert seen["fresh"] is True  # the thesis locks against what was fetched


def test_report_excerpts_render_as_text_not_markup(client):
    _seed("KO", "steady staple", 3, "hold")
    p = reporting.report_path("KO", store.today())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# KO report\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                 "Risk factor: <img src=x onerror=alert(1)> and R&D\n")
    store.mark_reported(store.find_entry("KO"))  # view must render THIS file, not regenerate
    r = client.get("/report/KO")
    assert r.status_code == 200
    assert "<img" not in r.text and "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "<table>" in r.text and "R&amp;D" in r.text  # markdown still renders


def test_report_links_only_to_safe_url_schemes(client):
    # Escaping the input stops raw tags, but markdown builds anchors from
    # `[text](url)` — and a filing can write `[click](javascript:...)`.
    _seed("KO", "steady staple", 3, "hold")
    p = reporting.report_path("KO", store.today())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# KO report\n\n[a](javascript:alert(1)) [b](data:text/html,x) "
                 "[c](VBscript:x) [ok](https://www.sec.gov/x) [rel](/reports/x.md)\n")
    store.mark_reported(store.find_entry("KO"))
    r = client.get("/report/KO")
    assert r.status_code == 200
    for scheme in ("javascript:", "data:text/html", "vbscript:", "VBscript:"):
        assert scheme not in r.text
    assert r.text.count("#blocked-url") == 3
    assert 'href="https://www.sec.gov/x"' in r.text and 'href="/reports/x.md"' in r.text


def test_url_scheme_allowlist_keeps_relative_targets(client):
    # A scheme allowlist, not a prefix guess: an unschemed target is relative
    # and stays, and control characters cannot smuggle a scheme past it
    # (browsers strip them, so this must too).
    from app import web

    for keep in ("docs/report.md", "x.md?q=1", "./a", "../a", "/a", "#a",
                 "https://sec.gov/x", "HTTP://sec.gov/x", "mailto:a@b.c"):
        assert web._safe_url(keep) == keep, keep
    for block in ("javascript:alert(1)", "JaVaScRiPt:x", "data:text/html,x",
                  "vbscript:x", "file:///etc/passwd", "ftp://x/y",
                  "java\nscript:alert(1)", "\tjavascript:x", " javascript:x"):
        assert web._safe_url(block) == "#blocked-url", block


def test_impact_refused_before_the_report_exists(client):
    _seed("KO", "steady staple", 3, "hold")
    r = client.post("/impact/KO", data={"verdict": "helped", "what_happened": "guided down"})
    assert r.status_code == 303 and "Generate+the+report" in r.headers["location"]
    e = store.parse_entry(store.find_entry("KO"))
    assert not e.get("verdict") and not e.get("what_happened")
    # ...and the refusal is shown where the user lands, not dropped.
    r = client.get(r.headers["location"])
    assert r.status_code == 200 and "Generate the report before recording its impact" in r.text


def test_report_generation_failure_leaves_entry_unreported(client, monkeypatch):
    def fail_build(ticker, with_docs=True, report_day=None, fresh=False):
        raise RuntimeError("missing EDGAR_IDENTITY")

    monkeypatch.setattr(reporting, "build_report", fail_build)
    _seed("CRM", "margin reset credible", 3, "hold")

    r = client.get("/report/CRM")

    assert r.status_code == 200
    assert "Report generation failed" in r.text
    assert not store.parse_entry(store.find_entry("CRM"))["is_reported"]


def test_report_for_dated_entry_reads_same_file_it_generates(client):
    _seed("KO", "steady staple", 3, "hold")
    p = store.find_entry("KO")
    old = p.with_name("KO_2026-01-15.md")
    p.rename(old)

    r = client.get("/report/KO?date=2026-01-15")

    assert r.status_code == 200
    assert "KO report" in r.text
    assert reporting.report_path("KO", "2026-01-15").exists()
    assert store.parse_entry(old)["is_reported"]


def test_impact_saves_fields(client):
    _seed("KO", "steady staple", 3, "hold")
    client.get("/report/KO")  # the AFTER block is only writable once the report exists
    r = client.post("/impact/KO", data={"impact": "changed_confidence", "conviction_after": "4",
                                        "verdict": "helped", "what_happened": "guided down"})
    assert r.status_code == 303
    e = store.parse_entry(store.find_entry("KO"))
    assert e["impact"] == "changed_confidence" and e["conviction_after"] == "4" and e["verdict"] == "helped"


def test_impact_form_renders(client):
    # GET /impact renders impact.html — the template not otherwise exercised by tests.
    _seed("KO", "steady staple", 3, "hold")
    r = client.get("/impact/KO")
    assert r.status_code == 200
    assert "Impact" in r.text and "Verdict" in r.text and "changed_thesis" in r.text


def test_dashboard_with_entries_renders(client):
    # Exercises the queue/table markup that only appears once cases exist.
    _seed("KO", "steady staple", 3, "hold")
    r = client.get("/")
    assert r.status_code == 200
    assert "KO" in r.text and "report pending" in r.text  # unreported case shows in the queue


def test_missing_entry_redirects(client):
    assert client.get("/impact/NOPE").status_code == 303
    assert client.get("/report/NOPE").status_code == 303


def test_no_web_path_writes_outside_the_entries_dir(client, tmp_path):
    for bad in ("../../../pwned", "..%2Fpwned", "PWNED/../x"):
        client.post("/open", data={"ticker": bad, "thesis": "x", "conviction": 3})
        client.get(f"/report/{bad}")
        client.get(f"/impact/{bad}")
    assert not list(tmp_path.rglob("*pwned*")) and not list(tmp_path.rglob("*PWNED*"))


def test_conviction_after_is_a_validated_select(client):
    _seed("KO", "steady staple", 3, "hold")
    r = client.get("/impact/KO")
    assert '<select id="conviction_after"' in r.text
    # options 1-5 present, free-text input is gone
    for n in range(1, 6):
        assert f'value="{n}"' in r.text
    assert 'input id="conviction_after"' not in r.text


def test_unreported_report_links_get_loading_class(client):
    _seed("KO", "steady staple", 3, "hold")
    r = client.get("/")
    assert "js-gen" in r.text and "data-loading-text" in r.text


def test_dashboard_shows_stale_outcome_banner(client):
    _seed("KO", "steady staple", 3, "hold")
    p = store.find_entry("KO")
    store.mark_reported(p)
    store.set_field(p, "impact", "no_value")
    store.set_field(p, "reported", "2020-01-01T00:00:00Z")

    r = client.get("/")
    assert "awaiting outcome" in r.text.lower()
    assert "outcome overdue" in r.text.lower()


def test_dashboard_omits_reported_but_after_not_filled_from_stale_banner(client):
    # Codex review catch: a reported case with a stale timestamp but an empty
    # AFTER block must show as "after needed", never inflate the outcome banner.
    _seed("KO", "steady staple", 3, "hold")
    p = store.find_entry("KO")
    store.mark_reported(p)
    store.set_field(p, "reported", "2020-01-01T00:00:00Z")  # old, AFTER left blank

    r = client.get("/")
    assert "awaiting outcome" not in r.text.lower()
    assert "after needed" in r.text.lower()


def test_impact_rejects_forged_conviction_after(client):
    # Codex review catch: the <select> only ever submits 1-5, but a forged POST
    # or curl call could send anything. The route must reject it at the boundary.
    _seed("KO", "steady staple", 3, "hold")
    client.get("/report/KO")  # AFTER fields are only writable once the report exists
    r = client.post("/impact/KO", data={"impact": "no_value", "conviction_after": "99"})
    assert r.status_code == 303
    assert store.parse_entry(store.find_entry("KO"))["conviction_after"] is None

    r2 = client.post("/impact/KO", data={"impact": "no_value", "conviction_after": "not-a-number"})
    assert r2.status_code == 303
    assert store.parse_entry(store.find_entry("KO"))["conviction_after"] is None

    # a legitimate value still writes normally
    client.post("/impact/KO", data={"impact": "no_value", "conviction_after": "4"})
    assert store.parse_entry(store.find_entry("KO"))["conviction_after"] == "4"


def _seed_v2(locked: bool = True, ticker: str = "MXL", day: str = "2026-07-27"):
    """A preregistered (v2) case, written the way `openv2` writes one."""
    from datetime import date, datetime, timezone

    from app.services.journal.schema_v2 import (
        Assumption, BeforeBlock, EntryV2, lock_entry,
    )

    entry = EntryV2(
        ticker=ticker,
        day=date.fromisoformat(day),
        opened=datetime(2026, 7, 27, 9, 41, tzinfo=timezone.utc),
        before=BeforeBlock(
            thesis="One of three optical DSP suppliers.",
            conviction=4,
            intended_action="hold",
            assumptions=[Assumption(
                metric="revenue", comparator=">", threshold=1_000_000_000.0,
                window="FY2026Q2", source="10-Q", resolve_by=date(2026, 8, 15))],
        ),
    )
    if locked:
        entry = lock_entry(entry)
    return store.save_v2(entry)


class TestV2ReadOnly:
    """v2 entries were invisible in the web UI: the dashboard tallies only v1,
    and every write route parses v1 markdown. Invisible is worse than plain —
    a hash-locked case is the only kind that can become evidence."""

    def test_dashboard_lists_preregistered_cases(self, client):
        _seed_v2()
        r = client.get("/")
        assert r.status_code == 200
        assert "Preregistered cases" in r.text and "MXL" in r.text
        assert "locked" in r.text and "2026-07-27" in r.text

    def test_unlocked_and_tampered_cases_are_labelled(self, client):
        p = _seed_v2(locked=False, ticker="AAA")
        assert "unlocked" in client.get("/").text
        p.unlink()
        p2 = _seed_v2(locked=True, ticker="BBB")
        p2.write_text(p2.read_text().replace("One of three", "Rewritten after the fact"))
        assert "lock broken" in client.get("/").text

    def test_an_unreadable_entry_does_not_take_the_dashboard_down(self, client, tmp_path):
        _seed_v2()
        (store.ENTRIES / "CCC_2026-07-28.md").write_text(
            "---\nschema_version: 2\n---\n{not json at all")
        r = client.get("/")
        assert r.status_code == 200 and "MXL" in r.text

    def test_write_routes_refuse_a_v2_case_and_say_where_to_go(self, client):
        _seed_v2()
        for url in ("/report/MXL", "/impact/MXL"):
            r = client.get(url)
            assert r.status_code == 303 and r.headers["location"].startswith("/?error=")
            assert "journal.py" in r.headers["location"]
        r = client.post("/impact/MXL", data={"verdict": "helped"})
        assert r.status_code == 303 and "journal.py" in r.headers["location"]
        # nothing was written into the locked file
        entry = store.load_v2(store.find_entry("MXL"))
        assert entry.after.impact is None and entry.outcome.verdict is None

    def test_the_dashboard_shows_the_refusal(self, client):
        _seed_v2()
        loc = client.get("/report/MXL").headers["location"]
        r = client.get(loc)
        assert r.status_code == 200 and "preregistered (v2) case" in r.text
