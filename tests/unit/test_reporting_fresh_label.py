"""`fresh` must reach the full report builder, whose data-quality line says
whether caches were bypassed — a fresh run was being labeled cache-eligible."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.journal import reporting


def test_fresh_reaches_the_report_builder(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        reporting, "SecClient",
        lambda fresh=False: SimpleNamespace(fresh=fresh, submissions=lambda ticker: {}))
    monkeypatch.setattr(
        reporting, "fetch_dataset_snapshot",
        lambda ticker, n_quarters, client: SimpleNamespace(
            dataset=SimpleNamespace(documents=None),
            diagnostics=SimpleNamespace(coverage=lambda: {}, warnings=[], selected_tags=lambda: {}),
            company_facts={}))
    monkeypatch.setattr(reporting, "analyze", lambda dataset: SimpleNamespace())
    monkeypatch.setattr(reporting, "describe", lambda t: "ok")

    def full(result, dataset, **kw):
        seen.update(kw)
        return "# report", SimpleNamespace()
    monkeypatch.setattr(reporting, "build_full_report", full)
    out, _ = reporting.build_report("NVDA", with_docs=False, fresh=True, out_dir=tmp_path)
    assert seen["fresh"] is True and out.read_text().startswith("# report")
