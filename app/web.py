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
import ipaddress
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import markdown as md
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from app.services.journal import reporting, review, store
from app.services.reporting.report_files import PublishInDoubt, recording

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Decision-Impact Journal", docs_url=None, redoc_url=None)

log = logging.getLogger(__name__)

# The names this UI answers to. It is a loopback tool; a page on any other
# name that resolves here is a DNS-rebinding page, same-origin with itself,
# which could read a case and post a forged tick (review of 2f26846,
# finding 1). A deployment reached by another name (through a proxy on this
# machine) lists it here.
ALLOWED_HOSTS_ENV = "FQE_WEB_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")
# The UI has no authentication, and its cross-site guard (`_foreign`) only
# protects a UI no other machine can reach: a header-less request from
# another machine passes it, and browsers send no Sec-Fetch-* to a plain
# http origin that is not loopback (review of 68dbc24, M-1). So only this
# machine's clients are served, unless the operator sets this to "1"; only
# behind a proxy that authenticates.
ALLOW_REMOTE_ENV = "FQE_WEB_ALLOW_REMOTE"
# A DNS name (labels of letters, digits and inner hyphens, no trailing dot)
# or an IPv4 address; IPv6 literals are checked apart.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class BadAllowedHosts(ValueError):
    """An entry of ``FQE_WEB_ALLOWED_HOSTS`` that names no host."""


def _allowed_hosts() -> set[str]:
    """The served names: ``FQE_WEB_ALLOWED_HOSTS`` (comma-separated, read
    through the same parser as a Host header, so a port on an entry is
    ignored; review of efb8500, L4), else the loopback names. An entry that
    is not a host name (`*`, a space, markup, a trailing dot, an IPv6 zone;
    review of 68dbc24, N-4) raises `BadAllowedHosts`: never a list that
    silently matches nothing."""
    named = set()
    for raw in os.environ.get(ALLOWED_HOSTS_ENV, "").split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry.count(":") > 1 and not entry.startswith("["):
            entry = f"[{entry}]"  # a bare IPv6 address
        host = _host_name(entry)
        if not (host and (_NAME_RE.match(host) or _ipv6(host))):
            raise BadAllowedHosts(f"{ALLOWED_HOSTS_ENV} entry {raw.strip()!r} is not a host "
                                  "name (a DNS name or an IP address; a port is ignored)")
        named.add(host)
    return named or set(DEFAULT_ALLOWED_HOSTS)


def _ipv6(host: str) -> bool:
    if "%" in host:
        return False  # a zone names an interface, not a host a browser sends
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def _host_name(header: str) -> str | None:
    """The host a ``Host`` header names, or None when it is not a bare
    ``host[:port]`` (no user, path, or malformed IPv6 literal or port)."""
    try:
        parts = urlsplit("//" + header)
        _ = parts.port  # a port that is not a number in 0-65535 raises ValueError
    except ValueError:
        return None
    if parts.username is not None or parts.path or parts.query or parts.fragment:
        return None
    return parts.hostname


def _local_client(request: Request) -> bool:
    """The request comes from this machine (a loopback address), or the
    operator has said to serve others (``FQE_WEB_ALLOW_REMOTE=1``)."""
    if os.environ.get(ALLOW_REMOTE_ENV) == "1":
        return True
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False  # not an address (a unix socket, a test client): not known local
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _foreign(request: Request) -> str | None:
    """Who sent ``request``, when it is not a page of this UI: a page on
    another site can post a form to a UI on loopback, or load one of its
    pages as an image. A browser says so on every request it makes for a
    page (``Sec-Fetch-Site``) and on every form POST (``Origin``). A
    request with neither is not from a browser page (curl, a script the
    operator runs on this machine), which no other site can make on their
    behalf, and is accepted: sound only because no other machine is served
    (`_local_client`). The Host itself is one of the served names."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return f"a page on another site (Sec-Fetch-Site: {site})"
    origin = request.headers.get("origin")
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin is not None and origin.lower() != own.lower():
        return f"another site ({origin})"
    return None


def _refuse_foreign(foreign: str) -> PlainTextResponse:
    return PlainTextResponse(f"Refused: a request from {foreign}. Use this UI's own pages.",
                             status_code=403)


@app.middleware("http")
async def _guard(request: Request, call_next):
    """Every request: from this machine (review of 68dbc24, M-1), to a
    served Host (DNS rebinding); and every request that changes something
    (anything but GET/HEAD/OPTIONS) from this UI's own pages only (reviews
    of 2f26846, finding 1, and efb8500, N2). A report's first view, a GET
    that builds and stamps, is checked by its page (`report_view`)."""
    if not _local_client(request):
        return PlainTextResponse(
            "This UI serves only this machine: it has no authentication. Reach it from "
            "elsewhere through `ssh -L 8000:127.0.0.1:8000`, or set "
            f"{ALLOW_REMOTE_ENV}=1 behind a proxy that authenticates.", status_code=403)
    host = _host_name(request.headers.get("host", ""))
    try:
        allowed = _allowed_hosts()
    except BadAllowedHosts as e:
        # Said to the operator in the log; to a request only that the
        # configuration is wrong, and only on a loopback name (anything
        # else is refused as always).
        log.error("%s", e)
        if host not in DEFAULT_ALLOWED_HOSTS:
            return PlainTextResponse("Invalid host header.", status_code=400)
        return PlainTextResponse(f"Misconfigured: {ALLOWED_HOSTS_ENV} holds an entry that is "
                                 "not a host name; the server log names it.", status_code=500)
    if host not in allowed:
        return PlainTextResponse(
            "Invalid host header: this UI answers only to the names it serves "
            f"(loopback, or those {ALLOWED_HOSTS_ENV} names).", status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        foreign = _foreign(request)
        if foreign is not None:
            return _refuse_foreign(foreign)
    return await call_next(request)


OPENV2_HINT = 'scripts/journal.py openv2 <TICKER> --thesis "..." --conviction 3'


def _read_failure(path) -> str | None:
    """Why `path` cannot be read, or None if it can."""
    try:
        with path.open("rb") as f:
            f.read(1)
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


def _v2_rows() -> list[dict]:
    """Preregistered (v2) entries, read-only. The web UI never wrote these and
    no longer writes any entry; without this they were simply invisible here,
    which is worse than plain — a hash-locked case is the only kind that can
    become evidence."""
    from app.services.journal.schema_v2 import open_assumption_indices, verify_lock

    rows: list[dict] = []
    for p in store.list_entries():
        if not store.is_v2(p):
            # `is_v2` answers False for a file it cannot read, which would
            # make an unreadable entry vanish from the one surface meant to
            # show every locked case. Unreadable is a state worth seeing.
            # Ask by reading, not by permission bits: `os.access` says yes to
            # root, and nothing about an I/O error or a bad mount.
            reason = _read_failure(p)
            if reason is not None:
                rows.append({"ticker": p.stem, "day": "", "unreadable": f"cannot be read ({reason})"})
            continue
        try:
            e = store.load_v2(p)
        except (ValueError, OSError) as exc:
            rows.append({"ticker": p.stem, "day": "", "unreadable": str(exc)})
            continue
        locked = e.locked_at is not None
        rows.append({
            "ticker": e.ticker,
            "day": e.day.isoformat(),
            "thesis": e.before.thesis,
            "conviction": e.before.conviction,
            "locked": locked,
            "lock_broken": locked and not verify_lock(e),
            "reported": e.reported is not None,
            "assumptions": len(e.before.assumptions),
            "open_assumptions": len(open_assumption_indices(e)),
            "unreadable": None,
        })
    rows.sort(key=lambda r: (r["day"], r["ticker"]), reverse=True)
    return rows


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"t": store.tally(), "v2": _v2_rows(), "openv2_hint": OPENV2_HINT, "error": error},
    )


@app.get("/open", response_class=HTMLResponse)
def open_form(request: Request, error: str | None = None):
    # The v1 form is retired, not repaired: a v1 entry has no hash-locked
    # BEFORE block, so it can never be preregistered evidence. New cases are
    # opened with `openv2` on the CLI, which locks the block as it writes it.
    return templates.TemplateResponse(
        request, "open.html", {"error": error, "openv2_hint": OPENV2_HINT})


@app.post("/open")
def open_submit(
    ticker: str = Form(""),
    thesis: str = Form(""),
    conviction: int = Form(3),
    action: str = Form("hold"),
):
    # Refused at the boundary, so a stale bookmark or a forged POST cannot
    # create a format we have retired.
    return RedirectResponse(
        "/open?error=The+web+form+no+longer+opens+cases.+Use+openv2+on+the+CLI.",
        status_code=303,
    )


@app.get("/report/{ticker}", response_class=HTMLResponse)
def report_view(request: Request, ticker: str, date: str | None = None,
                error: str | None = None):
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if path is None:
        return RedirectResponse("/", status_code=303)
    if store.is_v2(path):
        return RedirectResponse(
            "/?error=" + quote_plus(
                f"{store.safe_ticker(ticker)} is a preregistered (v2) case. The web UI shows "
                "it read-only; generate its report with `scripts/journal.py report "
                f"{store.safe_ticker(ticker)} --date {path.stem.split('_', 1)[1]}` so the "
                "lock is verified."),
            status_code=303)
    entry = store.parse_entry(path)
    if not entry["has_thesis"]:
        return RedirectResponse("/open?error=Write+a+thesis+before+generating+a+report.", status_code=303)

    report_file = reporting.report_path(ticker, entry["day"])
    status = 200
    if not entry["is_reported"]:
        # A GET that builds, publishes and stamps: refused to another site's
        # page like a POST (an <img> could make it). Only here, where it
        # acts: a stamped report's view is a view (review of 68dbc24, L-1;
        # a path match in the middleware also missed a --root-path prefix).
        foreign = _foreign(request)
        if foreign is not None:
            return _refuse_foreign(foreign)
        # First view: generate the networked report, then lock the thesis. Serialize
        # per entry and re-check under the lock so a double-request generates once.
        # The entry's report lock, the one `journal.py report` holds (Hermes
        # audit of 424b0b4, finding 3b): an in-process lock let this route and
        # the CLI each build and publish one entry's report. It also serves the
        # double-click it was for: each holder opens its own descriptor, so
        # two request threads exclude each other as two processes do.
        with store.report_lock(path):
            if not store.is_reported(path.read_text(encoding="utf-8")):
                problem, status = _generate_and_stamp(path, ticker, entry["day"])
                error = problem or error
    html = _render_report(report_file.read_text()) if report_file.exists() else None
    return templates.TemplateResponse(
        request, "report.html",
        {"entry": store.parse_entry(path), "report_html": html, "error": error},
        status_code=status,
    )


def _generate_and_stamp(path: Path, ticker: str, day: str) -> tuple[str | None, int]:
    """The first view's report of an unstamped v1 entry: built, then the
    thesis stamped. The caller holds the entry's report lock. Returns the
    page's error (None) and status."""
    def cli(command: str) -> str:
        return f"scripts/journal.py {command} {store.safe_ticker(ticker)} --date {day}"

    pending = store.pending_marker(path)
    if pending is not None:
        gen = "" if pending.generation_id is None else f" --generation {pending.generation_id}"
        if pending.not_stamped:
            # Its report was published and its stamp failed (here or by
            # `journal.py report`): nobody is at work on it (review of
            # 40c2d36, L1). Refused: nothing is built.
            return (f"This case's report was published but its thesis was NOT stamped: "
                    f"{pending.text}. Not building another over it. Stamp the published run: "
                    f"`{cli('mark-reported')}{gen}`; or rebuild it on purpose (the new run "
                    f"replaces it as the live one): `{cli('report')} --retry`."), 409
        # `journal.py report --defer-mark` published this report and the
        # sweep is auditing it; it stamps it once the audit passes (review of
        # the finding-3b fix: the page built and published over the run being
        # audited, and stamped it). Refused: nothing is built.
        return (f"This case's report is pending (being audited by the sweep, or left by an "
                f"interrupted run): {pending.text}. It is stamped once the audit passes; not "
                f"building another over it. Once the audit has passed: "
                f"`{cli('mark-reported')}{gen}`; to rebuild it on purpose (once its owner is "
                f"gone): `{cli('report')} --retry`."), 409
    try:
        # fresh=True: the first report LOCKS the thesis against what was
        # fetched. A <24h EDGAR cache can still hold pre-filing data on a
        # filing day; never lock on that.
        with recording() as published:
            reporting.build_report(ticker, with_docs=True, report_day=day, fresh=True)
    except PublishInDoubt as e:
        # The new report MAY be live (and is what the page shows, if so):
        # said as it is, as a server error, and the thesis is not locked.
        return f"Report publish IN DOUBT: {e}", 500
    except Exception as e:  # noqa: BLE001
        return f"Report generation failed: {e}", 200
    # Not in the build's `try`: a stamp that failed was shown as "Report
    # generation failed", with the report live.
    try:
        store.mark_reported(path)
    except Exception as e:  # noqa: BLE001
        return _not_stamped(path, e, published[-1] if published else None,
                            cli("mark-reported"))
    return None, 200


def _not_stamped(path: Path, e: Exception, made, mark: str) -> tuple[str, int]:
    """The page's stamp raised after its report went live; as `journal.py
    report` says it (review of 6563168, finding 4: the page left nothing
    pending, so opening it again built and published over the live run).
    A stamp in place on disk (its directory's fsync failed after the rename)
    is a stamp, said with its durability unconfirmed. Otherwise the entry is
    left PENDING, recording ``made`` (the run the build published, its only
    publish), so the page refuses to build again, and the command to stamp
    it names that run."""
    landed = store.reported_on_disk(path)
    if landed is not None:
        return (f"The thesis is stamped reported ({landed}), but its write raised after "
                f"the stamp was in place ({e}): the stamp could not be confirmed durable. "
                "Check the disk."), 200
    gen = "" if made is None else f" --generation {made.generation_id}"
    in_place, e2 = store.mark_not_stamped(
        path, "the web report page (its report was published; the stamp failed)",
        store.ReportOwner.this_process("the web report page"),
        None if made is None else made.generation_id,
        None if made is None else str(made.report))
    if e2 is None:
        kept = "It is left pending: this page will not build it again."
    elif in_place:
        kept = (f"Its pending marker is in place, but its write could not be confirmed "
                f"durable ({e2}): this page will not build it again.")
    else:
        kept = (f"Its pending marker could not be written either ({e2}): opening this page "
                "again builds its report again.")
    return (f"The report was generated and is live (below), but the thesis was NOT "
            f"stamped reported: {e}. {kept} Stamp it with `{mark}{gen}`."), 500


_URL_ATTR_RE = re.compile(r'''(\s(?:href|src)\s*=\s*)(["'])([^"']*)\2''', re.I)
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_SAFE_SCHEMES = {"http", "https", "mailto"}
_BLOCKED_URL = "#blocked-url"


def _safe_url(raw: str) -> str:
    """Keep relative targets and the three schemes a report legitimately
    needs; neutralize everything else. A URL with NO scheme is relative
    (`docs/x.md`, `./x`, `/x`, `#x`) and is kept as-is. Whitespace and
    control characters are removed before the scheme is read, because
    browsers strip them too: `java\\nscript:` is `javascript:` to Chrome."""
    probe = "".join(ch for ch in raw if ord(ch) > 0x20 and ord(ch) != 0x7F)
    m = _SCHEME_RE.match(probe)
    if m is None:
        return raw  # relative
    return raw if m.group(1).lower() in _SAFE_SCHEMES else _BLOCKED_URL


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
    html = md.markdown(_html.escape(markdown_text, quote=False),
                       extensions=["tables", "sane_lists"])
    # Escaping the input stops raw tags, but markdown builds its OWN anchors
    # from `[text](url)` — and a filing can write `[click](javascript:...)`.
    # Only http(s), mailto and relative targets survive; anything else
    # (javascript:, data:, vbscript:, file:) loses its target.
    #
    # Rewriting attributes with a regex is sound HERE only because the input
    # was escaped first, so the sole markup in `html` is what markdown itself
    # emitted. If this renderer is ever exposed beyond loopback, or ever
    # stops escaping its input, replace it with a parsed allowlist sanitizer.
    return _URL_ATTR_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_safe_url(m.group(3))}{m.group(2)}", html)


@app.get("/impact/{ticker}", response_class=HTMLResponse)
def impact_form(request: Request, ticker: str, date: str | None = None):
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/", status_code=303)
    if path is None:
        return RedirectResponse("/", status_code=303)
    if store.is_v2(path):
        return RedirectResponse(
            "/?error=" + quote_plus(
                f"{store.safe_ticker(ticker)} is a preregistered (v2) case. Record its AFTER "
                "block with `scripts/journal.py after`, which verifies the lock first."),
            status_code=303)
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
    if store.is_v2(path):
        return RedirectResponse("/?error=" + quote_plus(
            f"{store.safe_ticker(ticker)} is a preregistered (v2) case; use "
            "`scripts/journal.py after`."), status_code=303)
    if not store.is_reported(path.read_text(encoding="utf-8")):
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


# --- the review console: supervised shadow runs (app.services.journal.review) ---------
# Read-only over the published runs; its one write is the reviewer's ticks.


def _refused(request: Request, e: review.Refused, title: str, back: str | None = None):
    return templates.TemplateResponse(
        request, "review_refused.html", {"title": title, "message": e.message, "back": back},
        status_code=e.status)


@app.get("/review", response_class=HTMLResponse)
def review_board(request: Request):
    rows, problems = review.board()
    return templates.TemplateResponse(
        request, "review_board.html", {"rows": rows, "problems": problems})


@app.get("/review/{ticker}", response_class=HTMLResponse)
def review_case(request: Request, ticker: str, date: str | None = None):
    try:
        case = review.case(ticker, date)
    except review.Refused as e:
        return _refused(request, e, "Case not shown")
    audit = case.run.audit_text
    return templates.TemplateResponse(request, "review_case.html", {
        "case": case, "states": review.STATES, "note_max": review.NOTE_MAX,
        # Both untrusted (filer-quoted excerpts, a model-written audit):
        # `_render_report` escapes them before markdown, as on /report.
        "report_html": _render_report(case.run.live.text),
        "audit_html": _render_report(audit) if audit is not None else None,
    })


@app.post("/review/{ticker}/reconcile")
def review_reconcile(
    request: Request,
    ticker: str,
    date: str = Form(""),
    generation: str = Form(""),
    key: str = Form(""),
    state: str = Form(""),
    note: str = Form(""),
):
    # A tick from another site's page never reaches here (`_guard`).
    try:
        t, d = review.record_tick(ticker, date, generation, key, state, note)
    except review.Refused as e:
        back = None
        try:
            t, d = review.case_names(ticker, date)
            back = f"/review/{t}?date={d}"
        except review.Refused:
            pass
        return _refused(request, e, "Tick not recorded", back)
    return RedirectResponse(f"/review/{t}?date={d}#{key}", status_code=303)


_EXPORTS = {"csv": ("text/csv; charset=utf-8", "csv"),
            "md": ("text/markdown; charset=utf-8", "md")}


@app.get("/review/{ticker}/export")
def review_export(request: Request, ticker: str, date: str | None = None,
                  fmt: str = Query("csv", alias="format")):
    try:
        t, d = review.case_day(ticker, date)
        if fmt not in _EXPORTS:
            raise review.Refused(400, f"format {fmt!r}: csv or md")
        text = review.export(review.case(t, d), fmt)
    except review.Refused as e:
        return _refused(request, e, "Nothing exported")
    media, ext = _EXPORTS[fmt]
    return Response(text, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{t}_{d}_review.{ext}"'})
