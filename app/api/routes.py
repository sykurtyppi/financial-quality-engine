"""API routes: analyze a dataset, or analyze and render the markdown report."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.core.pipeline import analyze
from app.schemas.financials import CompanyDataset
from app.schemas.report import AnalysisResult
from app.services.reporting.markdown_report import render

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/analyze", response_model=AnalysisResult)
def analyze_dataset(dataset: CompanyDataset) -> AnalysisResult:
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
    return render(result, generated_on=date.today().isoformat())
