from decimal import Decimal
import os
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app import models  # noqa: F401  # Ensure models are imported before create_all.
from app.car_normalization import normalize_brand_model
from app.estimate_queries import (
    SQL_AVAILABLE_CARS_FROM_CLEAN,
    confidence_from_sample_size,
    fetch_exact_cohort_stats,
    fetch_exact_cohort_stats_by_reference_id,
    fetch_model_fallback_stats,
    fetch_sibling_fallback_stats_by_reference_id,
    percentiles_to_ints,
    scale_percentiles,
)
app = FastAPI(title="Seen Ad Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://seenad.ir",
        "https://www.seenad.ir",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.middleware("http")
async def log_estimate_payload(request: Request, call_next):
    if request.url.path == "/api/estimate-price" and request.method.upper() == "POST":
        body = await request.body()
        body_text = body.decode("utf-8", errors="replace")
        print(f"[estimate-price payload] {body_text}")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive)
    return await call_next(request)


class EstimateRequest(BaseModel):
    """Send either ``car_reference_id`` (preferred, from reference-tree trims) or ``brand`` + ``model``."""

    brand: str = ""
    model_name: str | None = None
    model: str | None = None
    model_year: int | str | None = None
    year: int | str | None = None
    mileage: int | str
    car_reference_id: int | None = None


class PricePercentiles(BaseModel):
    """Distribution from listings_clean cohort (IRR)."""

    p10: int | None = None
    p25: int | None = None
    median: int | None = None
    p75: int | None = None
    p90: int | None = None


class EstimateResponse(BaseModel):
    estimated_price: int
    min_price: int
    max_price: int
    currency: str = "IRR"
    sample_size: int
    strategy: str
    percentiles: PricePercentiles | None = None
    confidence_level: str = Field(description="Tier code from sample depth (PRD-aligned bands)")
    confidence_label: str = Field(description="Persian label for confidence tier")


_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_SPACE_RE = re.compile(r"\s+")
MIN_MODEL_SAMPLE_SIZE = int(os.getenv("ESTIMATE_MIN_SAMPLE_SIZE", "3"))
EXACT_COHORT_MIN_SAMPLES = int(os.getenv("ESTIMATE_EXACT_MIN_SAMPLES", "3"))
SIBLING_PRIMARY_WEIGHT = float(os.getenv("ESTIMATE_SIBLING_PRIMARY_WEIGHT", "2.0"))
REFERENCE_BRAND_ALIAS: dict[str, str] = {
    "peugeot": "پژو",
    "jac": "جک",
    "mvm": "ام وی ام",
    "samand": "سمند",
    "pride": "پراید",
    "dena": "دنا",
}


def normalize_numeric_input(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    normalized = str(value).translate(_FA_AR_TO_EN_DIGITS).strip()
    digits_only = re.sub(r"[^0-9]", "", normalized)
    if not digits_only:
        return None
    return int(digits_only)


def normalize_text_for_matching(value: str | None) -> str:
    if not value:
        return ""
    normalized = str(value).translate(_FA_AR_TO_EN_DIGITS).replace("\u200c", " ").strip().lower()
    return _SPACE_RE.sub(" ", normalized)


def canonicalize_brand_for_reference(value: str) -> str:
    key = normalize_text_for_matching(value)
    return REFERENCE_BRAND_ALIAS.get(key, value)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health_check(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database is not reachable") from exc


@app.get("/api/v1/available-cars", response_model=dict[str, list[str]])
def available_cars() -> dict[str, list[str]]:
    """Catalog listing moved to Main Server after Phase C DB cleanup."""
    raise HTTPException(
        status_code=503,
        detail="available-cars is served by the Main Server; scraper DB no longer has listings_clean.",
    )


@app.post("/api/estimate-price", response_model=EstimateResponse)
def estimate_price(payload: EstimateRequest, db: Session = Depends(get_db)) -> EstimateResponse:
    del payload, db
    raise HTTPException(
        status_code=503,
        detail="Price estimation is served by the Main Server after Phase C scraper DB cleanup.",
    )
