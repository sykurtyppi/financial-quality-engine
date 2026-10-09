"""Local web UI: the workbench (the way in) and the decision-impact journal.

The workbench (r36) is the product's front door on the operator's own
machine: type a ticker and see its decision card, the watchlist, a ticker's
report history, and a price box that produces the valuation shadow card. It
runs `reporting.build_report`, the CLI's own publish path, and reads what
that path writes (`app.services.workbench`); it changes no score and no
report text.

The journal pages (``/journal``, ``/report``, ``/impact``) and the review
console (``/review``) are the dogfooding tools they were: the journal wraps
the CLI loop (open thesis -> generate report -> record impact -> outcome)
over the identical ``journal/entries/*.md`` files via
``app.services.journal.store``.

    export EDGAR_IDENTITY="Your Name you@example.com"
    python scripts/ui.py                 # opens http://127.0.0.1:8000
"""

from __future__ import annotations

import html as _html
import ipaddress
import logging
import os
import re
from datetime import UTC, datetime
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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.services.ingestion.fields import FILING_CURRENCY
from app.services.journal import reporting, review, store
from app.services.reporting.report_files import PublishInDoubt, recording
from app.services.valuation.observation import (
    MarketObservation,
    eastern_today,
    find_observation,
    parse_observed_at,
    remove_observation,
    write_observation,
)
from app.services.valuation.render import SECTION_TITLE as VALUATION_TITLE
from app.services.workbench import jobs, setup, views, watching

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="FQE Workbench", docs_url=None, redoc_url=None)
# The stylesheet and the status poller. Behind `_guard` like every page: the
# middleware wraps the whole app, mounts included.
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

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
# A DNS name: labels of 1-63 letters, digits, inner hyphens, no trailing dot.
# Underscores too: browsers send a name that has one (`my_host.lan`) as it
# is, and refusing it would only make the operator's own name fail. A name
# whose last label is numeric is an IPv4 address or nothing (review of
# eeb1e51, N-3); IPv6 literals are checked apart.
_NAME_RE = re.compile(
    r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$")
# The FQE_WEB_ALLOWED_HOSTS values already said to be wrong in the log.
_LOGGED_BAD: set[str] = set()


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
        if not (host and (_ipv6(host) or _ipv4(host) or (
                _NAME_RE.match(host) and len(host) <= 253
                and not host.rsplit(".", 1)[-1].isdigit()))):
            raise BadAllowedHosts(f"{ALLOWED_HOSTS_ENV} entry {raw.strip()!r} is not a host "
                                  "name (a DNS name, a non-ASCII one in its xn-- punycode "
                                  "form, or an IP address; a port is ignored)")
        named.add(_canonical(host))
    return named or set(DEFAULT_ALLOWED_HOSTS)


def _ipv4(host: str) -> bool:
    """A dotted-quad IPv4 address, as `ipaddress` reads one: `127.1`,
    `0127.0.0.1` and `0x7f.0.0.1` are not (a browser rewrites or refuses
    them, so as served names they would match nothing)."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def _canonical(host: str) -> str:
    """An IP address in its one spelling (`::ffff:127.0.0.1` and
    `::ffff:7f00:1` are one address); a name as it is."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


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


# Said on every response (Hermes audit of PR #118, finding 4): no page of
# this UI may be framed. Framed by another site, a page could have a click
# steered onto "Record price" or "Remove from watchlist" (clickjacking): the
# Origin check passes a click made on the page itself. `frame-ancestors` is
# the CSP way to say it; X-Frame-Options, for a browser that predates it.
# Nothing broader: a fuller policy would have to be checked against every
# page's inline styles first.
FRAME_HEADERS = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}


@app.middleware("http")
async def _guard(request: Request, call_next):
    """Every request goes through `_admit`; every response, its refusals
    included, says it may not be framed (`FRAME_HEADERS`)."""
    refused = _admit(request)
    response = refused if refused is not None else await call_next(request)
    for name, value in FRAME_HEADERS.items():
        response.headers[name] = value
    return response


def _admit(request: Request) -> Response | None:
    """Why ``request`` is refused, as the response to send, or None. Every
    request: from this machine (review of 68dbc24, M-1), to a served Host
    (DNS rebinding); and every request that changes something (anything
    but GET/HEAD/OPTIONS) from this UI's own pages only (reviews of
    2f26846, finding 1, and efb8500, N2), building a journal case's report
    included (`report_build`: a POST since Hermes's audit of PR #118,
    finding 5)."""
    if not _local_client(request):
        return PlainTextResponse(
            "This UI serves only this machine: it has no authentication. Reach it from "
            "elsewhere through `ssh -L 8000:127.0.0.1:8000`, or set "
            f"{ALLOW_REMOTE_ENV}=1 behind a proxy that authenticates.", status_code=403)
    host = _host_name(request.headers.get("host", ""))
    host = _canonical(host) if host else host
    try:
        allowed = _allowed_hosts()
    except BadAllowedHosts as e:
        # Said to the operator in the log, once per value (review of
        # eeb1e51, N-4); to a request only that the configuration is wrong,
        # and only on a loopback name (anything else is refused as always).
        value = os.environ.get(ALLOWED_HOSTS_ENV, "")
        if value not in _LOGGED_BAD:
            _LOGGED_BAD.add(value)
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
    return None


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
            # What the operator declared reading before the thesis
            # (`openv2 --contamination`): the AFTER block measures the
            # engine net of it, so a reader of the row must see it (Hermes
            # audit of PR #118, finding 6).
            "contamination": e.before.contamination,
            "unreadable": None,
        })
    rows.sort(key=lambda r: (r["day"], r["ticker"]), reverse=True)
    return rows


@app.get("/journal", response_class=HTMLResponse)
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


def _report_case(ticker: str, date: str | None) -> tuple[Path, dict] | RedirectResponse:
    """The v1 journal case whose report the page shows or builds, with its
    parsed entry; or where to send the browser instead (no such case, a v2
    case, a case without a thesis)."""
    try:
        path = store.find_entry(ticker, date)
    except ValueError:
        return RedirectResponse("/journal", status_code=303)
    if path is None:
        return RedirectResponse("/journal", status_code=303)
    if store.is_v2(path):
        return RedirectResponse(
            "/journal?error=" + quote_plus(
                f"{store.safe_ticker(ticker)} is a preregistered (v2) case. The web UI shows "
                "it read-only; generate its report with `scripts/journal.py report "
                f"{store.safe_ticker(ticker)} --date {path.stem.split('_', 1)[1]}` so the "
                "lock is verified."),
            status_code=303)
    entry = store.parse_entry(path)
    if not entry["has_thesis"]:
        return RedirectResponse("/open?error=Write+a+thesis+before+generating+a+report.", status_code=303)
    return path, entry


def _report_page(request: Request, path: Path, ticker: str, day: str, error: str | None,
                 status: int = 200) -> HTMLResponse:
    report_file = reporting.report_path(ticker, day)
    html = _render_report(report_file.read_text()) if report_file.exists() else None
    return templates.TemplateResponse(
        request, "report.html",
        {"entry": store.parse_entry(path), "report_html": html, "error": error},
        status_code=status,
    )


@app.get("/report/{ticker}", response_class=HTMLResponse)
def report_view(request: Request, ticker: str, date: str | None = None,
                error: str | None = None):
    """A case's report, as it is: the live report, or, before one is
    built, the page with its "Build report" form. Nothing else: a GET that
    built, published and stamped on first view did so for a prefetch, a
    link preview or another site's <img> (Hermes audit of PR #118, finding
    5; the page's own cross-site check stood in for the guard's)."""
    found = _report_case(ticker, date)
    if isinstance(found, RedirectResponse):
        return found
    path, entry = found
    return _report_page(request, path, ticker, entry["day"], error)


@app.post("/report/{ticker}")
def report_build(request: Request, ticker: str, date: str | None = Form(None)):
    """Build the case's report and lock its thesis, then back to the view
    (303). Refused to another site's page by `_guard`, as every POST is. A
    refusal or a failure is said on the page with its status, as the first
    view said it. Serialized per entry and re-checked under the lock, so a
    double click builds once: the entry's report lock, the one `journal.py
    report` holds (Hermes audit of 424b0b4, finding 3b: an in-process lock
    let this route and the CLI each build and publish one entry's report).
    Each holder opens its own descriptor, so two request threads exclude
    each other as two processes do."""
    found = _report_case(ticker, date)
    if isinstance(found, RedirectResponse):
        return found
    path, entry = found
    problem, status = None, 200
    with store.report_lock(path):
        if not store.is_reported(path.read_text(encoding="utf-8")):
            problem, status = _generate_and_stamp(path, ticker, entry["day"])
    if problem is None:
        return RedirectResponse(f"/report/{store.safe_ticker(ticker)}?date={entry['day']}",
                                status_code=303)
    return _report_page(request, path, ticker, entry["day"], problem, status)


def _generate_and_stamp(path: Path, ticker: str, day: str) -> tuple[str | None, int]:
    """The report of an unstamped v1 entry (`report_build`): built, then
    the thesis stamped. The caller holds the entry's report lock. Returns
    the page's error (None) and status."""
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
        return RedirectResponse("/journal", status_code=303)
    if path is None:
        return RedirectResponse("/journal", status_code=303)
    if store.is_v2(path):
        return RedirectResponse(
            "/journal?error=" + quote_plus(
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
        return RedirectResponse("/journal", status_code=303)
    if path is None:
        return RedirectResponse("/journal", status_code=303)
    if store.is_v2(path):
        return RedirectResponse("/journal?error=" + quote_plus(
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
    return RedirectResponse("/journal", status_code=303)


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


# --- the workbench (r36): a ticker in, a decision card out ------------------------------
# Everything it writes goes through the CLI's own code: a run is
# `reporting.build_report` (`workbench.jobs`) into ``reports/workbench/``,
# never a journal case's live name; a price is the valuation module's
# validated writer, parsed as `market.py record` parses it; a watchlist add
# is `scripts/watch.py`'s `_arm`. Its runs are not the review console's
# (`/review` reviews the journal's), so a workbench card links to none.
# Every POST below is refused to another site's page by `_guard`, like the
# journal's.


def _utc(when: datetime | None) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if when is not None else "—"


templates.env.filters["utc"] = _utc


def _ticker(raw: str) -> str | None:
    """``raw`` as the journal names a ticker (`store.safe_ticker`), or None.
    Every path the workbench builds from a ticker is built from this."""
    try:
        return store.safe_ticker(raw)
    except ValueError:
        return None


def _not_a_ticker(raw: str) -> str:
    return (f"{raw!r} is not a ticker: 1-12 letters, digits, '.' or '-', starting with a "
            "letter or digit (e.g. KO, BRK.B).")


def _home(request: Request, *, error: str | None = None, msg: str | None = None,
          typed: str = "", status: int = 200) -> HTMLResponse:
    rows, watchlist_problem = views.watchlist_rows()
    return templates.TemplateResponse(request, "workbench_home.html", {
        "setup": setup.setup_problems(), "error": error, "msg": msg, "typed": typed,
        "rows": rows, "watchlist_problem": watchlist_problem, "recent": views.recent_runs(),
        "active": jobs.active(), "stale_days": views.RUN_STALE_DAYS,
    }, status_code=status)


@app.get("/", response_class=HTMLResponse)
def workbench_home(request: Request, error: str | None = None, msg: str | None = None):
    return _home(request, error=error, msg=msg)


@app.post("/t")
def ticker_submit(request: Request, ticker: str = Form("")):
    t = _ticker(ticker)
    if t is None:
        # Said back escaped (autoescape), and nothing is touched.
        return _home(request, error=_not_a_ticker(ticker), typed=ticker, status=400)
    return RedirectResponse(f"/t/{t}", status_code=303)


_TIER_BLOCK_RE = re.compile(
    r'<p><strong>Tier ([123]) — (.*?)</strong></p>\s*<ul>(.*?)</ul>', re.S)


def _decorate_card(html: str) -> str:
    """Classes on the card's tier headings, lists and warning lines, for the
    stylesheet: the report's markdown is the report's and is not changed.
    Sound for the reason `_render_report`'s link rewrite is: the input was
    escaped before markdown, so the only tags here are markdown's own."""
    def tier(m: re.Match[str]) -> str:
        n, title, items = m.group(1), m.group(2), m.group(3)
        items = items.replace("<li>⚠ not checked", '<li class="not-checked">⚠ not checked')
        items = items.replace("<li>none surfaced this run</li>",
                              '<li class="none">none surfaced this run</li>')
        return (f'<p class="tier tier-{n}"><strong>Tier {n} — {title}</strong></p>\n'
                f'<ul class="tier-list tier-{n}">{items}</ul>')
    html = _TIER_BLOCK_RE.sub(tier, html)
    return html.replace("<li>⚠ ", '<li class="warn">⚠ ')


def _card_lists(card: str) -> str:
    """The card's markdown with a blank line before each list that follows
    a line of text. The card writes a tier's flags straight under its bold
    heading (``**Tier 2 — ...:**`` then ``- flag``), which markdown reads as
    one paragraph: every flag ran into one line. For display only, and only
    on the card (engine text; the appendix quotes filers): the report file
    is not changed."""
    out: list[str] = []
    for line in card.splitlines():
        if line.startswith("- ") and out and out[-1].strip() and not out[-1].startswith("- "):
            out.append("")
        out.append(line)
    return "\n".join(out)


# The valuation shadow card's heading in the rendered appendix, given an id
# so the price box can link to it (`#valuation`).
_VALUATION_H2 = f"<h2>{_html.escape(VALUATION_TITLE.lstrip('# '), quote=False)}</h2>"


def _run_html(run: views.RunView) -> dict:
    appendix = _render_report(run.appendix) if run.appendix else None
    if appendix is not None:
        appendix = appendix.replace(_VALUATION_H2, _VALUATION_H2.replace("<h2>", '<h2 id="valuation">'), 1)
    return {"run": run, "card_html": _decorate_card(_render_report(_card_lists(run.card))),
            "appendix_html": appendix}


def _price_form(**typed: str) -> dict:
    """The price box's fields: what was typed (after a refusal, kept as it
    was), else the defaults. The time defaults to now on this machine's
    clock with its offset; app.js replaces it with the browser's own."""
    form = {"price": "", "source": "", "note": "",
            "observed_at": datetime.now().astimezone().isoformat(timespec="minutes"),
            "kept": False}
    if typed:
        form.update(typed, kept=True)
    return form


def _job_ctx(t: str) -> dict:
    """The run-status box's context: the ticker's newest run; the run queued
    to follow it (True when that one is fresh); whether a run was published
    since a failed run ended, which makes its failure old news (another
    process published, or an abandoned run ended after all); and the live
    run's generation, which a finished run is said against: "done" is not
    "live" once another run has published (Hermes audit of PR #118,
    finding 1)."""
    job = jobs.latest(t)
    failure_old = False
    if job is not None and job.state == jobs.FAILED:
        try:
            failure_old = views.published_since(t, job.finished_at or job.created_at)
        except (OSError, ValueError):
            failure_old = False
    live = None
    if job is not None and job.state in (jobs.DONE, jobs.SUPERSEDED):
        try:
            live = views.live_generation(t)
        except (OSError, ValueError):
            live = None
    return {"job": job, "t": t, "follow_up": jobs.follow_up(t), "failure_old": failure_old,
            "live_generation": live, "stall_minutes": jobs.STALL_AFTER_S // 60}


def _ticker_page(request: Request, t: str, *, error: str | None = None,
                 msg: str | None = None, status: int = 200,
                 form: dict | None = None) -> HTMLResponse:
    v = views.ticker_view(t)
    ctx: dict = {"v": v, "error": error, "msg": msg, "filing_currency": FILING_CURRENCY,
                 "form": form or _price_form(), "run": None, "card_html": None,
                 "appendix_html": None, "obs_age": None, "obs_stale": False, **_job_ctx(t)}
    if v.latest is not None:
        ctx.update(_run_html(v.latest))
    if v.observation is not None:
        today = eastern_today()  # the card counts the age on EDGAR's calendar
        ctx["obs_age"] = v.observation.observation.age_days(today)
        ctx["obs_stale"] = v.observation.observation.is_stale(today)
    return templates.TemplateResponse(request, "ticker.html", ctx, status_code=status)


@app.get("/t/{ticker}", response_class=HTMLResponse)
def ticker_page(request: Request, ticker: str, error: str | None = None, msg: str | None = None):
    t = _ticker(ticker)
    if t is None:
        return _home(request, error=_not_a_ticker(ticker), status=400)
    if t != ticker:
        return RedirectResponse(f"/t/{t}", status_code=303)
    return _ticker_page(request, t, error=error, msg=msg)


def _status(request: Request, t: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "_job_status.html", _job_ctx(t))


@app.post("/t/{ticker}/run")
def ticker_run(request: Request, ticker: str, fresh: str = Form("0")):
    t = _ticker(ticker)
    if t is None:
        return _home(request, error=_not_a_ticker(ticker), status=400)
    # "Refresh from SEC" bypasses the SEC cache (a filing-night fetch);
    # "Run" uses it, so a repeat view is fast. A refresh pressed while a
    # cached run is in flight follows it (`jobs.Registry.start`).
    jobs.start(t, fresh=fresh == "1")
    if request.headers.get("hx-request"):
        return _status(request, t)
    return RedirectResponse(f"/t/{t}", status_code=303)


@app.get("/t/{ticker}/status", response_class=HTMLResponse)
def ticker_status(request: Request, ticker: str):
    t = _ticker(ticker)
    if t is None:
        return PlainTextResponse(_not_a_ticker(ticker), status_code=400)
    return _status(request, t)


def _message(request: Request, title: str, message: str, back: str,
             status: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "workbench_message.html",
                                      {"title": title, "message": message, "back": back},
                                      status_code=status)


@app.get("/t/{ticker}/runs/{generation_id}", response_class=HTMLResponse)
def ticker_past_run(request: Request, ticker: str, generation_id: str):
    t = _ticker(ticker)
    if t is None:
        return _message(request, "No such run", _not_a_ticker(ticker), "/", 404)
    try:
        # Matched against the ticker's own listed generations; never joined
        # into a path (`views.past_run`).
        run = views.past_run(t, generation_id)
    except (OSError, ValueError) as e:
        return _message(request, "Run not shown", f"The run cannot be read: {e}", f"/t/{t}", 500)
    if run is None:
        return _message(request, "No such run",
                        f"{t} has no kept run with generation {generation_id!r}.", f"/t/{t}", 404)
    return templates.TemplateResponse(request, "workbench_run.html", _run_html(run))


def _invalid(e: ValidationError) -> str:
    """A refused observation's reasons, one per field, in pydantic's words."""
    return "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'observation'}: {err['msg']}"
                     for err in e.errors())


@app.post("/t/{ticker}/price")
def ticker_price(request: Request, ticker: str, price: str = Form(""),
                 currency: str = Form(FILING_CURRENCY), observed_at: str = Form(""),
                 source: str = Form(""), note: str = Form("")):
    t = _ticker(ticker)
    if t is None:
        return _home(request, error=_not_a_ticker(ticker), status=400)
    typed = {"price": price, "observed_at": observed_at, "source": source, "note": note}
    if currency != FILING_CURRENCY:
        # The box records the filing figures' currency only (Hermes audit
        # of PR #118, finding 2): it is a fixed field, so this is a forged
        # or stale form. The CLI still records another currency, and the
        # plane then derives nothing from it (`bridge.currency_mismatch`).
        return _ticker_page(
            request, t, status=400, form=_price_form(**typed),
            error=(f"Price not recorded: currency {currency!r}: the workbench records prices "
                   f"in {FILING_CURRENCY} only, the currency of the filing figures the card "
                   "sets a price against (no FX conversion). A price in another currency can "
                   "be recorded with `scripts/market.py record --currency`; the card then "
                   "shows no market cap, EV or multiple."))
    journal = reporting.MARKET.parent
    try:
        # Model assumptions and scenarios are recorded on the CLI only;
        # recording a price here keeps them, where `market.py record` would
        # replace the file whole. A file that cannot be read keeps nothing
        # (and is replaced, as `record` replaces it; a symlink is refused).
        kept = find_observation(journal, t)
    except (OSError, ValueError):
        kept = None
    try:
        # The time is parsed as `market.py record --at` parses it
        # (`parse_observed_at`): an ISO-8601 time with its offset. Handed
        # to the model as text, a bare number was read as Unix seconds
        # (review of 9d00328).
        at = parse_observed_at(observed_at.strip())
        # Validated by the model `market.py record` builds, from the text as
        # typed: a non-finite or non-positive price, a time after now,
        # control characters in any text are each refused there, not here.
        obs = MarketObservation.model_validate({
            "ticker": t, "price": price.strip(), "currency": currency,
            "observed_at": at, "source": source, "note": note or None,
            "recorded_at": datetime.now(UTC),
            "assumptions": kept.observation.assumptions if kept else None,
            "scenarios": kept.observation.scenarios if kept else (),
        })
    except ValidationError as e:
        return _ticker_page(request, t, error=f"Price not recorded: {_invalid(e)}",
                            status=400, form=_price_form(**typed))
    except ValueError as e:
        return _ticker_page(request, t, error=f"Price not recorded: {e}",
                            status=400, form=_price_form(**typed))
    try:
        write_observation(journal, obs)
    except OSError as e:
        return _ticker_page(request, t, error=f"Price not recorded: {e}", status=500,
                            form=_price_form(**typed))
    # A run already in flight read the old price as it started: one more
    # follows it.
    jobs.start(t, again=True)
    return RedirectResponse(f"/t/{t}", status_code=303)


@app.post("/t/{ticker}/price/remove")
def ticker_price_remove(request: Request, ticker: str):
    t = _ticker(ticker)
    if t is None:
        return _home(request, error=_not_a_ticker(ticker), status=400)
    try:
        removed = remove_observation(reporting.MARKET.parent, t)
    except (OSError, ValueError) as e:
        return RedirectResponse(f"/t/{t}?error=" + quote_plus(f"Price not removed: {e}"),
                                status_code=303)
    if removed is None:
        return RedirectResponse(f"/t/{t}?error=" + quote_plus(f"No price is recorded for {t}."),
                                status_code=303)
    # The live card still carries the old shadow card until a run without it.
    jobs.start(t, again=True)
    return RedirectResponse(f"/t/{t}", status_code=303)


@app.post("/watchlist")
def watchlist_submit(request: Request, ticker: str = Form(""), action: str = Form(""),
                     back: str = Form("home")):
    t = _ticker(ticker)
    if t is None:
        return _home(request, error=_not_a_ticker(ticker), status=400)
    if action not in ("add", "remove"):
        return _home(request, error=f"Unknown watchlist action {action!r}: add or remove.",
                     status=400)
    # Back to this UI's own page only, never a URL the form names.
    dest = f"/t/{t}" if back == "ticker" else "/"
    try:
        if action == "add":
            w = watching.add(t)
            msg = f"Added {t}: prints ~{_utc(w.print_at)} (a scheduling hint)."
        else:
            watching.remove(t)
            msg = f"Removed {t} from the watchlist."
    except watching.WatchRefused as e:
        return RedirectResponse(f"{dest}?error=" + quote_plus(str(e)), status_code=303)
    return RedirectResponse(f"{dest}?msg=" + quote_plus(msg), status_code=303)
