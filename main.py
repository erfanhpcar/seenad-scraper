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
from app.routers.vehicle_reference import router as vehicle_reference_router

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
app.include_router(vehicle_reference_router)


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
def available_cars(db: Session = Depends(get_db)) -> dict[str, list[str]]:
    """Brand/model pairs that exist in cleaned, mapped listings (Divar + Bama)."""
    rows = db.execute(SQL_AVAILABLE_CARS_FROM_CLEAN).mappings().all()

    cars: dict[str, list[str]] = {}
    for row in rows:
        brand = str(row["brand_name"]).strip()
        model = str(row["model_name"]).strip()
        cars.setdefault(brand, [])
        if model not in cars[brand]:
            cars[brand].append(model)
    return cars


@app.post("/api/estimate-price", response_model=EstimateResponse)
def estimate_price(payload: EstimateRequest, db: Session = Depends(get_db)) -> EstimateResponse:
    model_name = (payload.model_name or payload.model or "").strip()
    model_year = normalize_numeric_input(payload.model_year) or normalize_numeric_input(payload.year)
    mileage_value = normalize_numeric_input(payload.mileage)
    ref_id = payload.car_reference_id

    if model_year is None:
        raise HTTPException(status_code=422, detail="model_year/year is required")
    if mileage_value is None:
        raise HTTPException(status_code=422, detail="mileage is required")
    if model_year < 1300 or model_year > 1500:
        raise HTTPException(status_code=422, detail="model_year/year must be between 1300 and 1500")
    if mileage_value < 0 or mileage_value > 2_000_000:
        raise HTTPException(status_code=422, detail="mileage must be between 0 and 2000000")

    year_min = model_year - 1
    year_max = model_year + 1
    mileage_min = max(mileage_value - 40_000, 0)
    mileage_max = mileage_value + 40_000

    if ref_id is not None:
        if ref_id <= 0:
            raise HTTPException(status_code=422, detail="car_reference_id must be positive")
        if db.get(models.CarEntity, ref_id) is None:
            raise HTTPException(status_code=404, detail="مرجع خودرو یافت نشد")

        exact_count, exact_pct = fetch_exact_cohort_stats_by_reference_id(
            db,
            car_reference_id=ref_id,
            year_min=year_min,
            year_max=year_max,
            mileage_min=mileage_min,
            mileage_max=mileage_max,
        )

        if exact_count >= MIN_MODEL_SAMPLE_SIZE and exact_pct:
            pct_ints = percentiles_to_ints(exact_pct)
            median_est = pct_ints["median"]
            if median_est is None:
                median_est = int(round(exact_pct[2]))
            conf_code, conf_label = confidence_from_sample_size(exact_count)
            min_b = pct_ints["p10"] if pct_ints["p10"] is not None else int(median_est * 0.93)
            max_b = pct_ints["p90"] if pct_ints["p90"] is not None else int(median_est * 1.07)
            return EstimateResponse(
                estimated_price=median_est,
                min_price=min_b,
                max_price=max_b,
                sample_size=exact_count,
                strategy="exact",
                percentiles=PricePercentiles(**pct_ints),
                confidence_level=conf_code,
                confidence_label=conf_label,
            )

        sibling_count, sibling_pct, avg_mileage_d = fetch_sibling_fallback_stats_by_reference_id(
            db,
            car_reference_id=ref_id,
            primary_weight=SIBLING_PRIMARY_WEIGHT,
        )
        if sibling_count < MIN_MODEL_SAMPLE_SIZE or not sibling_pct:
            raise HTTPException(status_code=404, detail="دیتای کافی موجود نیست")

        avg_model_mileage = int(Decimal(str(avg_mileage_d)))
        mileage_gap = max(mileage_value - avg_model_mileage, 0)
        depreciation_ratio = min((mileage_gap / 10_000) * 0.012, 0.25)
        factor = Decimal(1) - Decimal(str(depreciation_ratio))
        scaled_pct = scale_percentiles(sibling_pct, factor)
        pct_ints = percentiles_to_ints(scaled_pct)
        median_est = pct_ints["median"]
        if median_est is None:
            median_est = int(round(float(sibling_pct[2]) * float(factor)))

        conf_code, conf_label = confidence_from_sample_size(sibling_count)
        min_b = pct_ints["p10"] if pct_ints["p10"] is not None else int(median_est * 0.9)
        max_b = pct_ints["p90"] if pct_ints["p90"] is not None else int(median_est * 1.1)

        return EstimateResponse(
            estimated_price=median_est,
            min_price=min_b,
            max_price=max_b,
            sample_size=sibling_count,
            strategy="sibling_fallback",
            percentiles=PricePercentiles(**pct_ints),
            confidence_level=conf_code,
            confidence_label=conf_label,
        )

    if not model_name:
        raise HTTPException(status_code=422, detail="model_name/model or car_reference_id is required")

    brand_value = payload.brand.strip()
    if not brand_value:
        raise HTTPException(status_code=422, detail="brand is required when car_reference_id is omitted")
    normalized = normalize_brand_model(brand_value, model_name)
    normalized_brand = canonicalize_brand_for_reference(normalized.brand_name or brand_value)
    normalized_model = normalized.model_name or model_name
    brand_match_key = normalize_text_for_matching(normalized_brand)
    model_match_key = normalize_text_for_matching(normalized_model)
    mapping_exists = db.execute(
        text(
            """
            SELECT 1
            FROM car_entities cr
            WHERE lower(btrim(cr.brand)) = :brand_exact
              AND lower(
                    btrim(
                        translate(cr.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
                    )
                  ) = :model_exact
            LIMIT 1
            """
        ),
        {
            "brand_exact": brand_match_key,
            "model_exact": model_match_key,
        },
    ).first()
    if mapping_exists is None:
        raise HTTPException(status_code=404, detail="دیتای کافی موجود نیست")

    exact_count, exact_pct = fetch_exact_cohort_stats(
        db,
        brand_exact=brand_match_key,
        model_exact=model_match_key,
        year_min=year_min,
        year_max=year_max,
        mileage_min=mileage_min,
        mileage_max=mileage_max,
    )

    if exact_count >= EXACT_COHORT_MIN_SAMPLES and exact_pct:
        pct_ints = percentiles_to_ints(exact_pct)
        median_est = pct_ints["median"]
        if median_est is None:
            median_est = int(round(exact_pct[2]))
        conf_code, conf_label = confidence_from_sample_size(exact_count)
        min_b = pct_ints["p10"] if pct_ints["p10"] is not None else int(median_est * 0.93)
        max_b = pct_ints["p90"] if pct_ints["p90"] is not None else int(median_est * 1.07)
        return EstimateResponse(
            estimated_price=median_est,
            min_price=min_b,
            max_price=max_b,
            sample_size=exact_count,
            strategy="exact",
            percentiles=PricePercentiles(**pct_ints),
            confidence_level=conf_code,
            confidence_label=conf_label,
        )

    fb_count, fb_pct, avg_mileage_d = fetch_model_fallback_stats(
        db,
        brand_exact=brand_match_key,
        model_exact=model_match_key,
    )

    if fb_count < MIN_MODEL_SAMPLE_SIZE or not fb_pct:
        raise HTTPException(status_code=404, detail="دیتای کافی موجود نیست")

    avg_model_mileage = int(Decimal(str(avg_mileage_d)))
    mileage_gap = max(mileage_value - avg_model_mileage, 0)
    depreciation_ratio = min((mileage_gap / 10_000) * 0.012, 0.25)
    factor = Decimal(1) - Decimal(str(depreciation_ratio))
    scaled_pct = scale_percentiles(fb_pct, factor)
    pct_ints = percentiles_to_ints(scaled_pct)
    median_est = pct_ints["median"]
    if median_est is None:
        median_est = int(round(float(fb_pct[2]) * float(factor)))

    conf_code, conf_label = confidence_from_sample_size(fb_count)
    min_b = pct_ints["p10"] if pct_ints["p10"] is not None else int(median_est * 0.9)
    max_b = pct_ints["p90"] if pct_ints["p90"] is not None else int(median_est * 1.1)

    return EstimateResponse(
        estimated_price=median_est,
        min_price=min_b,
        max_price=max_b,
        sample_size=fb_count,
        strategy="model_fallback",
        percentiles=PricePercentiles(**pct_ints),
        confidence_level=conf_code,
        confidence_label=conf_label,
    )
