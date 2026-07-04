"""Local web UI for the decision-impact journal — a dogfooding tool, not the product.

It wraps the exact CLI loop (open thesis -> generate report -> record impact ->
outcome) over the identical ``journal/entries/*.md`` files via
``app.services.journal.store``. No new capability, no scoring changes; it exists
only to reduce the friction of running the journal so it actually gets run.

    export EDGAR_IDENTITY="Your Name you@example.com"
    .venv/bin/uvicorn app.web:app        # then open http://127.0.0.1:8000
"""

from __future__ import annotations

from pathlib import Path

import markdown as md
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.journal import reporting, store

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Decision-Impact Journal", docs_url=None, redoc_url=None)


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
    except FileExistsError:
        return RedirectResponse(
            f"/open?error=An+entry+for+{ticker.upper()}+today+already+exists.", status_code=303
        )
    return RedirectResponse("/", status_code=303)


@app.get("/report/{ticker}", response_class=HTMLResponse)
def report_view(request: Request, ticker: str, date: str | None = None):
    path = store.find_entry(ticker, date)
    if path is None:
        return RedirectResponse("/", status_code=303)
    entry = store.parse_entry(path)
    if not entry["has_thesis"]:
        return RedirectResponse("/open?error=Write+a+thesis+before+generating+a+report.", status_code=303)

    report_file = reporting.report_path(ticker, entry["day"])
    error = None
    if not entry["is_reported"]:
        # First view: generate the networked report, then lock the thesis.
        try:
            reporting.build_report(ticker, with_docs=True, report_day=entry["day"])
            store.mark_reported(path)
        except Exception as e:  # noqa: BLE001
            error = f"Report generation failed: {e}"
    html = md.markdown(report_file.read_text(), extensions=["tables", "sane_lists"]) if report_file.exists() else None
    return templates.TemplateResponse(
        request, "report.html",
        {"entry": store.parse_entry(path), "report_html": html, "error": error},
    )


@app.get("/impact/{ticker}", response_class=HTMLResponse)
def impact_form(request: Request, ticker: str, date: str | None = None):
    path = store.find_entry(ticker, date)
    if path is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "impact.html",
        {"entry": store.parse_entry(path),
         "impact_codes": store.IMPACT_CODES, "verdicts": store.VERDICTS},
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
    path = store.find_entry(ticker, date)
    if path is None:
        return RedirectResponse("/", status_code=303)
    for key, val in (
        ("impact", impact), ("conviction_after", conviction_after),
        ("what_it_surfaced", what_it_surfaced), ("what_i_disagreed_with", what_i_disagreed_with),
        ("outcome_date", outcome_date), ("what_happened", what_happened), ("verdict", verdict),
    ):
        if val.strip():
            store.set_field(path, key, val.strip())
    return RedirectResponse("/", status_code=303)
