from __future__ import annotations

from collections import defaultdict
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CarEntity


class ReferenceTrimNode(BaseModel):
    """One row in car_entities (brand + model + trim)."""

    label: str
    car_reference_id: int


class ReferenceModelNode(BaseModel):
    label: str
    value: str
    trims: list[ReferenceTrimNode] = Field(default_factory=list)


class ReferenceBrandNode(BaseModel):
    label: str
    value: str
    models: list[ReferenceModelNode]


class ReferenceTreeResponse(BaseModel):
    brands: list[ReferenceBrandNode]


router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles-reference"])

_REFERENCE_CACHE_LOCK = Lock()
_REFERENCE_CACHE_TTL_SECONDS = 900.0
_reference_tree_cache: dict[str, object] = {"expires_at": 0.0, "data": None}


def _build_reference_tree(db: Session) -> ReferenceTreeResponse:
    rows = db.execute(
        select(
            CarEntity.id,
            CarEntity.brand,
            CarEntity.model,
            CarEntity.trim,
        )
        .where(CarEntity.brand.is_not(None), CarEntity.model.is_not(None))
        .where(CarEntity.brand != "", CarEntity.model != "")
        .order_by(
            CarEntity.brand,
            CarEntity.model,
            CarEntity.trim,
        )
    ).all()

    nested: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
    for ref_id, brand_raw, model_raw, trim_raw in rows:
        brand = brand_raw.strip()
        model = model_raw.strip()
        trim = trim_raw.strip() if trim_raw else ""
        if not brand or not model:
            continue
        if not trim:
            trim = "پایه"
        nested[brand][model].append((int(ref_id), trim))

    brands_out: list[ReferenceBrandNode] = []
    for brand in sorted(nested.keys(), key=lambda item: item.casefold()):
        models_out: list[ReferenceModelNode] = []
        for model in sorted(nested[brand].keys(), key=lambda item: item.casefold()):
            trims_pairs = sorted(nested[brand][model], key=lambda item: item[1].casefold())
            trims_out = [
                ReferenceTrimNode(label=trim_label, car_reference_id=tid) for tid, trim_label in trims_pairs
            ]
            models_out.append(ReferenceModelNode(label=model, value=model, trims=trims_out))
        brands_out.append(ReferenceBrandNode(label=brand, value=brand, models=models_out))

    return ReferenceTreeResponse(brands=brands_out)


@router.get("/reference-tree", response_model=ReferenceTreeResponse)
def vehicle_reference_tree(db: Session = Depends(get_db)) -> ReferenceTreeResponse:
    now = monotonic()

    cached_data = _reference_tree_cache.get("data")
    if isinstance(cached_data, ReferenceTreeResponse) and now < float(_reference_tree_cache["expires_at"]):
        return cached_data

    with _REFERENCE_CACHE_LOCK:
        now = monotonic()
        cached_data = _reference_tree_cache.get("data")
        if isinstance(cached_data, ReferenceTreeResponse) and now < float(_reference_tree_cache["expires_at"]):
            return cached_data

        fresh_data = _build_reference_tree(db)
        _reference_tree_cache["data"] = fresh_data
        _reference_tree_cache["expires_at"] = now + _REFERENCE_CACHE_TTL_SECONDS
        return fresh_data
