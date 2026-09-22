"""Repo-wide test guards."""

import os

import pytest
from hypothesis import HealthCheck, settings


@pytest.fixture(autouse=True)
def _never_notify_for_real(monkeypatch):
    # app.services.delivery.notify posts a real macOS notification; no test
    # may reach it unmocked. Tests that assert on notify patch it explicitly.
    monkeypatch.setenv("FQE_NO_NOTIFY", "1")


@pytest.fixture(autouse=True)
def _isolated_vintage_store(monkeypatch, tmp_path):
    # The report's silent-revision stream reads data/vintages/ when no root is
    # given. A dev box holding real captures would make every end-to-end test
    # that resolves a real CIK load and diff multi-MB snapshots. Tests that
    # want a store build one under tmp_path (or pass root= explicitly).
    from app.services.ingestion import vintages

    monkeypatch.setattr(vintages, "VINTAGES", tmp_path / "vintages")


@pytest.fixture(autouse=True)
def _strict_streams(monkeypatch):
    # In production a defect inside an evidence stream is contained and
    # labelled an internal error so the report still renders; in tests it
    # must fail loudly. Tests of the production labelling set this back to
    # False themselves.
    from app.services.reporting import report_builder

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", True)


# Property tests (hypothesis, dev-only). `ci` — the default — is derandomized
# and keeps no example database, so every run of the suite checks the same
# examples (hypothesis still caches source-mined constants under the
# gitignored .hypothesis/); `explore` searches far wider for a developer
# hunting a bug: HYPOTHESIS_PROFILE=explore pytest tests/unit/test_properties_*.py
settings.register_profile(
    "ci", derandomize=True, database=None, deadline=None, max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "explore", max_examples=2000, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
