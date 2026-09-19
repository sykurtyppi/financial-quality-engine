"""Local web UI for the decision-impact journal — a dogfooding tool, not the product.

It wraps the exact CLI loop (open thesis -> generate report -> record impact ->
outcome) over the identical ``journal/entries/*.md`` files via
``app.services.journal.store``. No new capability, no scoring changes; it exists
only to reduce the friction of running the journal so it actually gets run.

    export EDGAR_IDENTITY="Your Name you@example.com"
    .venv/bin/uvicorn app.web:app        # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import html as _html
import threading
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.journal import reporting, store

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Decision-Impact Journal", docs_url=None, redoc_url=None)

# Per-entry locks so concurrent GET /report requests (double-click, tab prefetch)
# don't each fire a redundant EDGAR fetch. Single-process local server only.
_gen_locks: dict[str, threading.Lock] = {}
_gen_guard = threading.Lock()


def _gen_lock(key: str) -> threading.Lock:
    with _gen_guard:
        return _gen_locks.setdefault(key, threading.Lock())


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"t": store.tally()})


@app.get("/open", response_class=HTMLResponse)
def open_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "open.html", {"error": error})


@app.post("/open")
def open_submit(
    ticker: str = Form(...),
    thesis: str = Form(...),
    conviction: int = Form(...),
    action: str = Form("hold"),
):
    if not thesis.strip():
        return RedirectResponse("/open?error=Write+your+thesis+first.", status_code=303)
    try:
        store.open_entry(ticker.strip(), thesis.strip(), conviction, action.strip())
    except ValueError:
        return RedirectResponse("/open?error=Invalid+ticker+symbol.", status_code=303)
    except FileExistsError:
        return RedirectResponse(
            f"/open?error=An+entry+for+{store.safe_ticker(ticker)}+today+already+exists.",
            status_code=303,
        )
    return RedirectResponse("/", status_code=303)


@app.get("/report/{ticker}", response_class=HTMLResponse)
def report_view(request: Request, ticker: str, date: str | None = None,
                error: str | None = None):
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if path is None:
        return RedirectResponse("/", status_code=303)
    entry = store.parse_entry(path)
    if not entry["has_thesis"]:
        return RedirectResponse("/open?error=Write+a+thesis+before+generating+a+report.", status_code=303)

    report_file = reporting.report_path(ticker, entry["day"])
    if not entry["is_reported"]:
        # First view: generate the networked report, then lock the thesis. Serialize
        # per entry and re-check under the lock so a double-request generates once.
        with _gen_lock(str(path)):
            if not store.is_reported(path.read_text()):
                try:
                    # fresh=True: the first report LOCKS the thesis against
                    # what was fetched. A <24h EDGAR cache can still hold
                    # pre-filing data on a filing day; never lock on that.
                    reporting.build_report(ticker, with_docs=True, report_day=entry["day"],
                                           fresh=True)
                    store.mark_reported(path)
                except Exception as e:  # noqa: BLE001
                    error = f"Report generation failed: {e}"
    html = _render_report(report_file.read_text()) if report_file.exists() else None
    return templates.TemplateResponse(
        request, "report.html",
        {"entry": store.parse_entry(path), "report_html": html, "error": error},
    )


def _render_report(markdown_text: str) -> str:
    """Markdown -> HTML for the template's `| safe` slot. The report quotes
    filer-authored excerpts (MD&A, risk factors, releases), so it is untrusted
    text: every `<`, `>` and `&` is escaped BEFORE markdown, which otherwise
    passes raw HTML straight through. Headings, tables and lists still
    render; a tag inside a filing renders as the literal characters.

    Known limit: markdown escapes the inside of code spans itself, so a
    backtick-quoted `&` would render double-escaped. Generated reports
    contain no code spans today; if one is ever added, switch to sanitizing
    the OUTPUT with an allow-list instead."""
    return md.markdown(_html.escape(markdown_text, quote=False),
                       extensions=["tables", "sane_lists"])


@app.get("/impact/{ticker}", response_class=HTMLResponse)
def impact_form(request: Request, ticker: str, date: str | None = None):
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if path is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "impact.html",
        {"entry": store.parse_entry(path),
         "impact_codes": store.IMPACT_CODES, "verdicts": store.VERDICTS,
         "conviction_choices": store.CONVICTION_CHOICES},
    )


@app.post("/impact/{ticker}")
def impact_submit(
    ticker: str,
    date: str | None = Form(None),
    impact: str = Form(""),
    conviction_after: str = Form(""),
    what_it_surfaced: str = Form(""),
    what_i_disagreed_with: str = Form(""),
    outcome_date: str = Form(""),
    what_happened: str = Form(""),
    verdict: str = Form(""),
):
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if path is None:
        return RedirectResponse("/", status_code=303)
    if not store.is_reported(path.read_text()):
        # AFTER/outcome fields recorded before the report exists are
        # hindsight, not evidence: the journal's whole point is a verdict
        # formed AFTER a locked thesis met the report. Refuse at the boundary.
        return RedirectResponse(
            f"/report/{store.safe_ticker(ticker)}?error=Generate+the+report+before+"
            "recording+its+impact.", status_code=303)
    for key, val in (
        ("impact", impact), ("conviction_after", conviction_after),
        ("what_it_surfaced", what_it_surfaced), ("what_i_disagreed_with", what_i_disagreed_with),
        ("outcome_date", outcome_date), ("what_happened", what_happened), ("verdict", verdict),
    ):
        val = val.strip()
        if not val:
            continue
        # conviction_after is a <select>; the browser can only submit 1-5. Reject
        # anything else at the boundary (a forged POST or hand-edited value)
        # rather than trust the client — this is the one field a stray value would
        # silently corrupt (see store.tally()'s isdigit()-guarded conviction math).
        if key == "conviction_after" and val not in store.CONVICTION_CHOICES:
            continue
        store.set_field(path, key, val)
    return RedirectResponse("/", status_code=303)
