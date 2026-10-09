"""`scripts/ui.py`: the one command that starts the workbench (r36).

It must never serve another machine (the UI has no authentication; `_guard`
refuses non-loopback clients, and the server should not even listen for
them), must say what to install when the `[web]` extra is missing, and must
start on a box with no EDGAR_IDENTITY (the home page explains the setup).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ui_cli", ROOT / "scripts" / "ui.py")
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)


@pytest.fixture
def started(monkeypatch):
    """uvicorn.run and the browser, recorded instead of run."""
    calls: dict = {"run": [], "open": [], "chdir": []}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls["run"].append((app, kw)))
    monkeypatch.setattr(ui.webbrowser, "open", lambda url, *a, **k: calls["open"].append(url))
    monkeypatch.setattr(ui, "_open_later", lambda url: ui.webbrowser.open(url))
    monkeypatch.setattr(ui.os, "chdir", lambda p: calls["chdir"].append(Path(p)))
    monkeypatch.setenv("EDGAR_IDENTITY", "Jane Doe jane@example.com")
    return calls


def test_defaults_serve_loopback_8000_and_open_the_browser(started, capsys):
    assert ui.main([]) == 0
    ((app, kw),) = started["run"]
    from app.web import app as web_app

    assert app is web_app
    assert kw["host"] == "127.0.0.1" and kw["port"] == 8000
    # As the runbook starts it: only this machine's proxy headers believed.
    assert kw["forwarded_allow_ips"] == "127.0.0.1"
    assert started["open"] == ["http://127.0.0.1:8000/"]
    # The SEC cache (data/cache) and every relative path are the checkout's.
    assert started["chdir"] == [ROOT]
    assert "http://127.0.0.1:8000/" in capsys.readouterr().out


def test_port_and_no_browser(started):
    assert ui.main(["--port", "8123", "--no-browser"]) == 0
    ((_, kw),) = started["run"]
    assert kw["port"] == 8123 and started["open"] == []


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", "", "127.0.0.2"])
def test_a_host_that_is_not_loopback_is_refused(started, host, capsys):
    with pytest.raises(SystemExit) as e:
        ui.main(["--host", host])
    assert e.value.code == 2 and started["run"] == []
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize("host,url", [("localhost", "http://localhost:8000/"),
                                      ("::1", "http://[::1]:8000/")])
def test_loopback_names_are_accepted(started, host, url):
    assert ui.main(["--host", host]) == 0
    assert started["run"][0][1]["host"] == host and started["open"] == [url]


def test_the_hosts_offered_are_the_ones_the_guard_serves():
    from app import web

    assert set(ui.LOOPBACK_HOSTS) == set(web.DEFAULT_ALLOWED_HOSTS)


@pytest.mark.parametrize("port", ["1", "65535"])
def test_the_port_range_is_inclusive(started, port):
    assert ui.main(["--port", port, "--no-browser"]) == 0
    assert started["run"][0][1]["port"] == int(port)


@pytest.mark.parametrize("port", ["0", "65536", "70000", "http"])
def test_a_bad_port_is_refused(started, port):
    with pytest.raises(SystemExit) as e:
        ui.main(["--port", port])
    assert e.value.code == 2 and started["run"] == []


def test_missing_web_extra_says_what_to_install(started, monkeypatch, capsys):
    monkeypatch.setattr(ui, "WEB_MODULES", (*ui.WEB_MODULES, "fqe_no_such_module_xyz"))
    assert ui.main(["--no-browser"]) == 2
    err = capsys.readouterr().err
    assert "fqe_no_such_module_xyz" in err and 'pip install -e ".[web]"' in err
    assert started["run"] == []


def test_no_identity_warns_and_still_starts(started, monkeypatch, capsys):
    monkeypatch.delenv("EDGAR_IDENTITY")
    assert ui.main(["--no-browser"]) == 0
    assert "EDGAR_IDENTITY" in capsys.readouterr().err and len(started["run"]) == 1


def test_the_real_browser_opener_waits_for_the_server(monkeypatch):
    """`_open_later` hands the URL to a timer, so the page is requested once
    the server is listening, not before."""
    seen = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            seen.append((delay, fn, args))
            self.daemon = False

        def start(self):
            seen.append("started")

    monkeypatch.setattr(ui.threading, "Timer", FakeTimer)
    ui._open_later("http://127.0.0.1:8000/")
    assert seen[0][0] > 0 and seen[0][1] is ui.webbrowser.open
    assert seen[0][2] == ("http://127.0.0.1:8000/",) and seen[1] == "started"


def test_the_script_is_executable_and_documented():
    assert os.access(ROOT / "scripts" / "ui.py", os.X_OK)
    readme = (ROOT / "README.md").read_text()
    assert "python scripts/ui.py" in readme
