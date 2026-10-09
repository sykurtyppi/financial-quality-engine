#!/usr/bin/env python3
"""Start the workbench: the local web app, in your browser.

    export EDGAR_IDENTITY="Your Name you@example.com"
    python scripts/ui.py                  # http://127.0.0.1:8000, opened for you
    python scripts/ui.py --port 8001 --no-browser

Type a ticker, and ~10-20 s later its decision card; the watchlist, each
ticker's report history, and a price box that adds the valuation shadow card
are one click away. Reports are published to reports/ exactly as
`scripts/generate_report.py` publishes them.

It serves this machine only: it listens on a loopback address (anything
else is refused here, and `app.web`'s guard refuses any other client), and
believes forwarded-for headers from 127.0.0.1 only, as the runbook starts
uvicorn (`--forwarded-allow-ips 127.0.0.1`). The UI has no authentication;
to use it from another machine, forward the port: `ssh -L 8000:127.0.0.1:8000`.

Runs from the checkout's root, so the SEC cache is `data/cache` there, as
for every other command. Exit codes: 0 stopped normally · 2 a bad argument,
or the `[web]` extra is not installed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The `[web]` extra (pyproject.toml): checked before app.web is imported,
# which would fail on the first of them with a bare ImportError.
WEB_MODULES = ("fastapi", "jinja2", "multipart", "markdown", "uvicorn")
# The names `app.web` serves by default (its DEFAULT_ALLOWED_HOSTS): a
# browser on any other loopback address (127.0.0.2) would send a Host the
# guard refuses, so only these are offered.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# Seconds before the browser is pointed at the page: the server is
# listening by then on any machine this runs on.
BROWSER_DELAY_S = 1.0


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is not a port number (1-65535)")
    return port


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1",
                   help="where to listen: 127.0.0.1 (default), ::1 or localhost. Anything "
                        "else is refused: the UI has no authentication")
    p.add_argument("--port", type=_port, default=8000, help="default 8000")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")
    return p


def missing_web_modules() -> list[str]:
    return [m for m in WEB_MODULES if importlib.util.find_spec(m) is None]


def _url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}/"


def _open_later(url: str) -> None:
    timer = threading.Timer(BROWSER_DELAY_S, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        parser.error(f"--host {args.host!r} is not a loopback address: the workbench has no "
                     "authentication and serves this machine only (reach it from elsewhere "
                     "with `ssh -L 8000:127.0.0.1:8000`)")
    missing = missing_web_modules()
    if missing:
        print(f"The web UI needs the [web] extra; missing: {', '.join(missing)}.\n"
              f'Install it: pip install -e ".[web]"   (from {ROOT})', file=sys.stderr)
        return 2
    if not os.environ.get("EDGAR_IDENTITY"):
        print('warning: EDGAR_IDENTITY is not set; runs will fail until it is (export '
              'EDGAR_IDENTITY="Your Name you@example.com" and restart). The home page says so '
              "too.", file=sys.stderr)
    os.chdir(ROOT)
    import uvicorn

    from app.web import app

    url = _url(args.host, args.port)
    print(f"FQE Workbench on {url} (Ctrl-C stops it)")
    if not args.no_browser:
        _open_later(url)
    uvicorn.run(app, host=args.host, port=args.port, forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
