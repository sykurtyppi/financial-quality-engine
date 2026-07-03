"""FastAPI application entry point.

Run locally:
    .venv/bin/uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Earnings Quality & Narrative Drift Engine",
    description=(
        "Deterministic earnings-quality screening. Outputs are formula-driven "
        "opinions for analyst review, not allegations or investment advice."
    ),
    version="0.1.0",
)
app.include_router(router)
