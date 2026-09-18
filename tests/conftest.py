"""Repo-wide test guards."""

import pytest


@pytest.fixture(autouse=True)
def _never_notify_for_real(monkeypatch):
    # app.services.delivery.notify posts a real macOS notification; no test
    # may reach it unmocked. Tests that assert on notify patch it explicitly.
    monkeypatch.setenv("FQE_NO_NOTIFY", "1")
