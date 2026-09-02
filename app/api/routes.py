"""API routes: analyze a dataset, or analyze and render the markdown report."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.core.pipeline import analyze
from app.schemas.financials import CompanyDataset
from app.schemas.report import AnalysisResult
from app.services.reporting.report_builder import build_report

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# NOTE: response_model_exclude strips `overall` from the JSON body only — the
# published OpenAPI schema still lists the field on AnalysisResult. Accepted
# limitation; a wire consumer never receives a value for it.
@router.post("/analyze", response_model=AnalysisResult, response_model_exclude={"overall"})
def analyze_dataset(dataset: CompanyDataset) -> AnalysisResult:
    """The composite 0-100 was measured non-discriminating over the 2026Q2
    season (11 live runs, zero decision value) and retired from every surface
    — the human report AND this machine-readable one. It survives internally
    only as a sort key; exposing it here would quietly re-create the product
    truth the retirement removed."""
    try:
        return analyze(dataset)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/report", response_class=PlainTextResponse)
def report_dataset(dataset: CompanyDataset) -> str:
    try:
        result = analyze(dataset)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # Review finding 1: the API returns the same decision card + appendix as the
    # CLI/journal. No client here (a dataset is posted), so evidence streams that
    # need EDGAR are omitted; the card + thermometer render from the dataset.
    report, _ = build_report(result, dataset, generated_on=date.today().isoformat())
    return report
