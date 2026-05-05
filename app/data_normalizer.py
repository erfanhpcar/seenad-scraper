from __future__ import annotations

import os
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.car_normalization import normalize_brand_model
from app.external_inspector import ExternalDbInspector
from app.models import ScrapedListing

TARGET_TABLES = ("Divar1", "TELLS", "Divar_j4_900_150")

# Unified Celery/mapping pipeline source (see postgres/migrations/006_normalize_divar_source.sql).
DIVAR_SOURCE = "divar"

_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def fa_to_en_num(value: Any) -> str:
    """
    Convert Persian/Arabic digits to English digits and remove separators.
    """
    if value is None:
        return ""
    text = str(value).translate(_FA_AR_TO_EN_DIGITS).strip()
    text = (
        text.replace("،", "")
        .replace(",", "")
        .replace("٬", "")
        .replace(" ", "")
        .replace("\u200c", "")
        .replace("\u200f", "")
        .replace("\u202b", "")
        .replace("\u202c", "")
    )
    return text


def _extract_int(value: Any) -> int | None:
    normalized = fa_to_en_num(value)
    if not normalized:
        return None
    digits = re.sub(r"[^0-9]", "", normalized)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _normalize_price(value: Any) -> int | None:
    return _extract_int(value)


def _normalize_mileage(value: Any) -> int | None:
    return _extract_int(value)


def _normalize_model_year(value: Any) -> int | None:
    return _extract_int(value)


def _price_for_db(price_int: int | None) -> Decimal | None:
    # scraped_listings.price is Numeric(14,2) -> max absolute integer part: 999,999,999,999
    if price_int is None:
        return None
    if price_int >= 1_000_000_000_000:
        return None
    return Decimal(price_int)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _fetch_external_rows(
    table_name: str,
    limit: int | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    inspector = ExternalDbInspector()
    connection = inspector._connect()
    try:
        cursor = connection.cursor(as_dict=True)
        if since is not None and limit is not None:
            cursor.execute(
                f"SELECT TOP {int(limit)} * FROM [{table_name}] WHERE [time] > %s ORDER BY [time] ASC",
                (since,),
            )
        elif since is not None:
            cursor.execute(f"SELECT * FROM [{table_name}] WHERE [time] > %s ORDER BY [time] ASC", (since,))
        elif limit is not None:
            cursor.execute(f"SELECT TOP {int(limit)} * FROM [{table_name}] ORDER BY [time] DESC")
        else:
            cursor.execute(f"SELECT * FROM [{table_name}]")
        return cursor.fetchall()
    finally:
        connection.close()


def _upsert_listing(db: Session, source: str, row: dict[str, Any], *, external_table: str) -> None:
    raw_external_id = str(row.get("ID") or "").strip()
    if not raw_external_id:
        return

    external_id = f"{external_table}:{raw_external_id}"

    normalized_car = normalize_brand_model(row.get("brand"), row.get("tip"))
    price_int = _normalize_price(row.get("price"))
    mileage_int = _normalize_mileage(row.get("mileage"))
    model_year_int = _normalize_model_year(row.get("model_year"))

    raw_data = _json_safe(dict(row))
    raw_data["external_table"] = external_table
    raw_data["normalized"] = {
        "brand_name": normalized_car.brand_name,
        "model_name": normalized_car.model_name,
        "trim_name": normalized_car.trim_name,
        "model_year": model_year_int,
        "price_int": price_int,
        "mileage_int": mileage_int,
    }

    scraped_at = row.get("time")
    if isinstance(scraped_at, str):
        try:
            scraped_at = datetime.fromisoformat(scraped_at)
        except ValueError:
            scraped_at = None

    stmt = insert(ScrapedListing).values(
        source=source,
        external_id=external_id,
        price=_price_for_db(price_int),
        mileage=mileage_int,
        city=(str(row.get("city")).strip() if row.get("city") is not None else None),
        brand_name=normalized_car.brand_name,
        model_name=normalized_car.model_name,
        trim_name=normalized_car.trim_name,
        raw_data=raw_data,
        created_at_source=scraped_at,
        scraped_at=scraped_at,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_scraped_listings_source_external_id",
        set_={
            "price": stmt.excluded.price,
            "mileage": stmt.excluded.mileage,
            "city": stmt.excluded.city,
            "brand_name": stmt.excluded.brand_name,
            "model_name": stmt.excluded.model_name,
            "trim_name": stmt.excluded.trim_name,
            "raw_data": stmt.excluded.raw_data,
            "created_at_source": stmt.excluded.created_at_source,
            "scraped_at": stmt.excluded.scraped_at,
        },
    )
    db.execute(stmt)


def _last_scraped_at_divar_table(db: Session, external_table: str) -> datetime | None:
    stmt = (
        select(func.max(ScrapedListing.scraped_at))
        .where(ScrapedListing.source == DIVAR_SOURCE)
        .where(ScrapedListing.raw_data.contains({"external_table": external_table}))
    )
    return db.execute(stmt).scalar_one_or_none()


def run_ingestion(limit_per_table: int | None = None, incremental: bool = True) -> int:
    """
    Pull raw rows from external SQL Server (Divar) and upsert into scraped_listings.
    """
    required_vars = ("EXT_DB_HOST", "EXT_DB_USER", "EXT_DB_PASS", "EXT_DB_NAME")
    missing = [key for key in required_vars if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    db = SessionLocal()
    try:
        total = 0
        for table_name in TARGET_TABLES:
            last_seen = _last_scraped_at_divar_table(db, table_name) if incremental else None
            rows = _fetch_external_rows(table_name, limit=limit_per_table, since=last_seen)

            for row in rows:
                _upsert_listing(db=db, source=DIVAR_SOURCE, row=row, external_table=table_name)
            db.commit()
            total += len(rows)
            if last_seen:
                print(f"Upserted {len(rows)} rows from {table_name} (incremental since {last_seen.isoformat()})")
            else:
                print(f"Upserted {len(rows)} rows from {table_name}")

        print(f"Total upserted rows: {total}")
        return total
    finally:
        db.close()


def run_normalization(limit_per_table: int | None = None, incremental: bool = True) -> None:
    """Full synchronous pipeline: ingestion → color/gearbox FKs → car_reference mapping."""
    run_ingestion(limit_per_table=limit_per_table, incremental=incremental)

    from app.listing_normalization import run_color_gearbox_normalization

    color_n, gear_n = run_color_gearbox_normalization()
    print(f"Normalization: color_id rows updated={color_n}, gearbox_id rows updated={gear_n}")

    from scripts.relink_from_mapping import run_relink_pipeline

    allow_seeds = os.getenv("NORMALIZER_ALLOW_REFERENCE_SEEDS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    summary = run_relink_pipeline(allow_car_reference_mutations=allow_seeds)
    print(f"Mapping pipeline summary: {summary}")


if __name__ == "__main__":
    # Optional limit for quick test:
    # NORMALIZER_LIMIT=100 python -m app.data_normalizer
    env_limit = os.getenv("NORMALIZER_LIMIT")
    incremental_enabled = os.getenv("NORMALIZER_INCREMENTAL", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    run_normalization(limit_per_table=int(env_limit) if env_limit else None, incremental=incremental_enabled)
