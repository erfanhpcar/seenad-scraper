from __future__ import annotations

import logging
import os
import re

import pymssql
import redis
import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from celery import chain
from celery.exceptions import Ignore

from app.body_condition import merge_body_condition_into_payload
from app.celery_app import celery_app
from app.data_normalizer import run_ingestion
from app.database import SessionLocal
from app.models import ScrapedListing
from app.scrapers.bama_detail_parser import BamaDetailExtract, fetch_and_parse_bama_listing
from app.scrapers.hm_scraper import HMDetailExtract, fetch_and_parse_hm_listing
from app.scrapers.karnameh_scraper import KNDetailExtract, fetch_and_parse_kn_listing
from app.tasks.normalization import (
    divar_mapping_relink_task,
    normalization_task,
    sync_listings_clean_task,
)

LOGGER = logging.getLogger(__name__)


def _ingestion_retry_exceptions() -> tuple[type[BaseException], ...]:
    # pymssql.OperationalError (20047 / DBPROCESS is dead) is a subclass of pymssql.Error.
    return (pymssql.Error, OSError, TimeoutError, ConnectionError)


def _bama_retry_exceptions() -> tuple[type[BaseException], ...]:
    """
    Exceptions that trigger Celery autoretry for bama_ingestion_task.

    - Network/HTTP while fetching HTML: ``RequestException``, ``ConnectionError``, etc.
    - Transient PostgreSQL connectivity in the worker: ``OperationalError`` (e.g. pool, DNS blip).
    """
    return (
        requests.RequestException,
        OSError,
        TimeoutError,
        ConnectionError,
        OperationalError,
    )


def _handle_status_circuit_breaker(task, *, input_url: str, exc: requests.HTTPError, task_name: str) -> None:
    status_code = exc.response.status_code if exc.response is not None else None
    if status_code == 429:
        LOGGER.warning(
            "%s received HTTP 429; applying circuit-breaker retry after 900s url=%s",
            task_name,
            input_url,
        )
        raise task.retry(exc=exc, countdown=900)
    if status_code == 403:
        LOGGER.critical(
            "CRITICAL: IP PROBABLY BANNED site=%s status=403 url=%s",
            task_name,
            input_url,
        )
        raise Ignore()


def _truncate_db_field(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_len]


def _pop_next_bama_url() -> str | None:
    queue_key = os.getenv("SEENAD_BAMA_URL_QUEUE_KEY", "seenad:bama:url_queue").strip() or "seenad:bama:url_queue"
    broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(broker, decode_responses=True)
        popped = client.lpop(queue_key)
        if popped:
            cleaned = popped.strip()
            if cleaned:
                return cleaned
    except redis.RedisError:
        LOGGER.warning("Redis unavailable while reading Bama URL queue.", exc_info=True)

    static_url = os.getenv("BAMA_INGESTION_URL", "").strip()
    return static_url or None


def _pop_next_hm_url() -> str | None:
    queue_key = os.getenv("SEENAD_HM_URL_QUEUE_KEY", "seenad:hm:url_queue").strip() or "seenad:hm:url_queue"
    broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(broker, decode_responses=True)
        popped = client.lpop(queue_key)
        if popped:
            cleaned = popped.strip()
            if cleaned:
                return cleaned
    except redis.RedisError:
        LOGGER.warning("Redis unavailable while reading HM URL queue.", exc_info=True)

    static_url = os.getenv("HM_INGESTION_URL", "").strip()
    return static_url or None


def _pop_next_kn_url() -> str | None:
    queue_key = os.getenv("SEENAD_KN_URL_QUEUE_KEY", "seenad:kn:url_queue").strip() or "seenad:kn:url_queue"
    broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(broker, decode_responses=True)
        popped = client.lpop(queue_key)
        if popped:
            cleaned = popped.strip()
            if cleaned:
                return cleaned
    except redis.RedisError:
        LOGGER.warning("Redis unavailable while reading KN URL queue.", exc_info=True)

    static_url = os.getenv("KN_INGESTION_URL", "").strip()
    return static_url or None


def _validate_bama_extract_for_persist(extract: BamaDetailExtract) -> None:
    """
    scraped_listings.external_id is NOT NULL.
    Price may be missing for some listings; keep ingesting metadata.
    """
    ext = (extract.external_id or "").strip()
    if not ext:
        LOGGER.error(
            "Bama persist validation failed: external_id empty after strip. listing_url=%s",
            extract.listing_url,
        )
        raise ValueError("bama extract has no external_id — cannot insert into scraped_listings")

    if extract.price is None:
        LOGGER.info(
            "Bama listing external_id=%r has no numeric price; continuing persist negotiable=%s note=%r",
            ext,
            extract.price_is_negotiable,
            extract.price_debug_note,
        )


def _build_bama_raw_payload(extract: BamaDetailExtract) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "bama",
        "url": extract.listing_url,
        "brand": extract.brand,
        "model": extract.model,
        "trim": extract.trim,
        "year": extract.year,
        "mileage": extract.mileage,
        "description": extract.description,
        "images": list(extract.images),
        "city": extract.city,
        "color": extract.color,
        "gearbox": extract.transmission,
        "transmission": extract.transmission,
        "price_is_negotiable": extract.price_is_negotiable,
        "price_debug_note": extract.price_debug_note,
    }
    return merge_body_condition_into_payload(
        payload,
        source="bama",
        raw=extract.body_condition,
        source_path=extract.body_condition_source_path,
    )


def _hm_has_parse_error(extract: HMDetailExtract) -> bool:
    if not (extract.external_id or "").strip():
        return True
    if not (extract.brand or "").strip() or not (extract.model or "").strip():
        return True
    if extract.price is None:
        return True
    if extract.mileage is None or extract.mileage < 0:
        return True
    return False


def _validate_hm_extract_for_persist(extract: HMDetailExtract) -> None:
    ext = (extract.external_id or "").strip()
    if not ext:
        LOGGER.error("HM persist validation failed: external_id empty. listing_url=%s", extract.listing_url)
        raise ValueError("hm extract has no external_id — cannot insert into scraped_listings")
    if _hm_has_parse_error(extract):
        LOGGER.warning(
            "HM persist with parse_error=true external_id=%r url=%s brand=%r model=%r price=%s mileage=%s",
            ext,
            extract.listing_url,
            extract.brand,
            extract.model,
            extract.price,
            extract.mileage,
        )


def _build_hm_raw_payload(extract: HMDetailExtract) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "hm",
        "url": extract.listing_url,
        "brand": extract.brand,
        "model": extract.model,
        "trim": extract.trim,
        "year": extract.year,
        "mileage": extract.mileage,
        "description": extract.description,
        "images": list(extract.images),
        "city": extract.city,
        "price_debug_note": extract.price_debug_note,
        "parse_error": _hm_has_parse_error(extract),
    }
    return merge_body_condition_into_payload(
        payload,
        source="hm",
        raw=extract.body_condition,
        source_path=extract.body_condition_source_path,
    )


def _is_noisy_text(value: str | None) -> bool:
    if value is None:
        return True
    compact = re.sub(r"\s+", " ", value).strip().lower()
    if not compact:
        return True
    if compact in {"-", "--", "unknown", "null", "none", "nan", "نامشخص", "بدون توضیحات"}:
        return True
    alnum = re.sub(r"[^\w\u0600-\u06ff]+", "", compact)
    return len(alnum) < 2


def _kn_has_parse_error(extract: KNDetailExtract) -> bool:
    if not (extract.external_id or "").strip():
        return True
    if extract.price is None:
        return True
    if _is_noisy_text(extract.brand) or _is_noisy_text(extract.model):
        return True
    useful_fields = [
        extract.trim,
        extract.body_condition,
        extract.description,
        extract.city,
        str(extract.year) if extract.year is not None else None,
        str(extract.mileage) if extract.mileage is not None else None,
    ]
    informative = [field for field in useful_fields if not _is_noisy_text(field)]
    if not informative and not extract.images:
        return True
    return False


def _validate_kn_extract_with_content_guard(extract: KNDetailExtract) -> None:
    ext = (extract.external_id or "").strip()
    if not ext:
        LOGGER.error("KN content guard failed: empty external_id. listing_url=%s", extract.listing_url)
        raise ValueError("kn extract has no external_id — cannot insert into scraped_listings")

    if _kn_has_parse_error(extract):
        LOGGER.warning(
            "KN persist with parse_error=true external_id=%r url=%s brand=%r model=%r price=%s",
            ext,
            extract.listing_url,
            extract.brand,
            extract.model,
            extract.price,
        )


def _build_kn_raw_payload(extract: KNDetailExtract) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "karnameh",
        "url": extract.listing_url,
        "brand": extract.brand,
        "model": extract.model,
        "trim": extract.trim,
        "year": extract.year,
        "mileage": extract.mileage,
        "description": extract.description,
        "images": list(extract.images),
        "city": extract.city,
        "price_debug_note": extract.price_debug_note,
        "parse_error": _kn_has_parse_error(extract),
    }
    return merge_body_condition_into_payload(
        payload,
        source="karnameh",
        raw=extract.body_condition,
        source_path=extract.body_condition_source_path,
    )


def _upsert_bama_scraped_listing(db: Session, extract: BamaDetailExtract) -> ScrapedListing:
    incoming_raw = _build_bama_raw_payload(extract)

    record = db.scalar(
        select(ScrapedListing).where(
            ScrapedListing.source == "bama",
            ScrapedListing.external_id == extract.external_id,
        )
    )

    if record:
        LOGGER.info(
            "bama scraped_listings upsert: UPDATE existing row id=%s external_id=%r",
            record.id,
            extract.external_id,
        )
        merged_raw = {**(record.raw_data or {}), **incoming_raw}
        record.price = extract.price
        record.mileage = extract.mileage
        record.city = _truncate_db_field(extract.city, 100)
        record.brand_name = _truncate_db_field(extract.brand, 100)
        record.model_name = _truncate_db_field(extract.model, 120)
        record.model_year = extract.year
        record.trim_name = _truncate_db_field(extract.trim, 120)
        record.created_at_source = extract.created_at_source
        record.raw_data = merged_raw
        return record

    LOGGER.info("bama scraped_listings upsert: INSERT new row external_id=%r", extract.external_id)
    listing = ScrapedListing(
        source="bama",
        external_id=extract.external_id,
        price=extract.price,
        mileage=extract.mileage,
        city=_truncate_db_field(extract.city, 100),
        brand_name=_truncate_db_field(extract.brand, 100),
        model_name=_truncate_db_field(extract.model, 120),
        model_year=extract.year,
        trim_name=_truncate_db_field(extract.trim, 120),
        created_at_source=extract.created_at_source,
        raw_data=incoming_raw,
    )
    db.add(listing)
    return listing


def _upsert_hm_scraped_listing(db: Session, extract: HMDetailExtract) -> ScrapedListing:
    incoming_raw = _build_hm_raw_payload(extract)
    hm_source = "hamrah-mechanic"
    record = db.scalar(
        select(ScrapedListing).where(
            ScrapedListing.source == hm_source,
            ScrapedListing.external_id == extract.external_id,
        )
    )

    if record:
        LOGGER.info("hm scraped_listings upsert: UPDATE existing row id=%s external_id=%r", record.id, extract.external_id)
        merged_raw = {**(record.raw_data or {}), **incoming_raw}
        record.price = extract.price
        record.mileage = extract.mileage
        record.city = _truncate_db_field(extract.city, 100)
        record.brand_name = _truncate_db_field(extract.brand, 100)
        record.model_name = _truncate_db_field(extract.model, 120)
        record.model_year = extract.year
        record.trim_name = _truncate_db_field(extract.trim, 120)
        record.created_at_source = extract.created_at_source
        record.raw_data = merged_raw
        return record

    LOGGER.info("hm scraped_listings upsert: INSERT new row external_id=%r", extract.external_id)
    listing = ScrapedListing(
        source=hm_source,
        external_id=extract.external_id,
        price=extract.price,
        mileage=extract.mileage,
        city=_truncate_db_field(extract.city, 100),
        brand_name=_truncate_db_field(extract.brand, 100),
        model_name=_truncate_db_field(extract.model, 120),
        model_year=extract.year,
        trim_name=_truncate_db_field(extract.trim, 120),
        created_at_source=extract.created_at_source,
        raw_data=incoming_raw,
    )
    db.add(listing)
    return listing


def _upsert_kn_scraped_listing(db: Session, extract: KNDetailExtract) -> ScrapedListing:
    incoming_raw = _build_kn_raw_payload(extract)
    kn_source = "karnameh"
    record = db.scalar(
        select(ScrapedListing).where(
            ScrapedListing.source == kn_source,
            ScrapedListing.external_id == extract.external_id,
        )
    )

    if record:
        LOGGER.info("kn scraped_listings upsert: UPDATE existing row id=%s external_id=%r", record.id, extract.external_id)
        merged_raw = {**(record.raw_data or {}), **incoming_raw}
        record.price = extract.price
        record.mileage = extract.mileage
        record.city = _truncate_db_field(extract.city, 100)
        record.brand_name = _truncate_db_field(extract.brand, 100)
        record.model_name = _truncate_db_field(extract.model, 120)
        record.model_year = extract.year
        record.trim_name = _truncate_db_field(extract.trim, 120)
        record.created_at_source = extract.created_at_source
        record.raw_data = merged_raw
        return record

    LOGGER.info("kn scraped_listings upsert: INSERT new row external_id=%r", extract.external_id)
    listing = ScrapedListing(
        source=kn_source,
        external_id=extract.external_id,
        price=extract.price,
        mileage=extract.mileage,
        city=_truncate_db_field(extract.city, 100),
        brand_name=_truncate_db_field(extract.brand, 100),
        model_name=_truncate_db_field(extract.model, 120),
        model_year=extract.year,
        trim_name=_truncate_db_field(extract.trim, 120),
        created_at_source=extract.created_at_source,
        raw_data=incoming_raw,
    )
    db.add(listing)
    return listing


@celery_app.task(
    bind=True,
    autoretry_for=_ingestion_retry_exceptions(),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    name="tasks.ingestion_task",
)
def ingestion_task(self) -> int:
    """
    Fetch from external SQL Server and upsert scraped_listings.
    Retries on broker/network/driver failures (pymssql errors, timeouts).

    After a successful ingest, enqueues Celery chain:
    normalization → Divar mapping-only relink → listings_clean sync.
    Disable with DIVAR_PIPELINE_CHAIN=0.
    """
    limit_env = os.getenv("NORMALIZER_LIMIT")
    limit = int(limit_env) if limit_env else None
    incremental = os.getenv("NORMALIZER_INCREMENTAL", "1").strip().lower() not in {"0", "false", "no"}
    total = run_ingestion(limit_per_table=limit, incremental=incremental)

    chain_enabled = os.getenv("DIVAR_PIPELINE_CHAIN", "0").strip().lower() not in {"0", "false", "no"}
    if chain_enabled:
        chain(
            normalization_task.si(),
            divar_mapping_relink_task.si(),
            sync_listings_clean_task.si(),
        ).delay()
        LOGGER.info(
            "ingestion_task enqueued Divar pipeline chain rows=%s (normalization → mapping apply → listings_clean)",
            total,
        )
    else:
        LOGGER.info("ingestion_task skipped DIVAR_PIPELINE_CHAIN rows=%s", total)

    return total


@celery_app.task(
    bind=True,
    autoretry_for=_bama_retry_exceptions(),
    retry_kwargs={"max_retries": 3, "countdown": 45},
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    name="tasks.bama_ingestion_task",
)
def bama_ingestion_task(self, url: str) -> dict[str, object]:
    """Fetch one Bama listing URL, parse detail HTML, upsert scraped_listings (source=bama, raw only)."""
    input_url = (url or "").strip()
    task_id = getattr(self.request, "id", None)
    LOGGER.info("bama_ingestion_task INPUT_URL=%s task_id=%s", input_url, task_id)

    if not input_url:
        LOGGER.error("bama_ingestion_task aborted: empty INPUT_URL")
        raise ValueError("bama_ingestion_task requires a non-empty url")

    try:
        extract = fetch_and_parse_bama_listing(input_url)
    except requests.HTTPError as exc:
        _handle_status_circuit_breaker(self, input_url=input_url, exc=exc, task_name="bama_ingestion_task")
        raise
    except _bama_retry_exceptions() as exc:
        LOGGER.warning(
            "bama_ingestion_task RETRY_TRIGGER kind=%s INPUT_URL=%s task_id=%s detail=%s",
            type(exc).__name__,
            input_url,
            task_id,
            exc,
            exc_info=True,
        )
        raise

    LOGGER.info(
        "bama_ingestion_task EXTRACT_SUMMARY external_id=%r price=%s mileage=%s city=%r "
        "brand=%r model=%r trim=%r negotiable=%s note=%r listing_url=%s",
        extract.external_id,
        extract.price,
        extract.mileage,
        extract.city,
        extract.brand,
        extract.model,
        extract.trim,
        extract.price_is_negotiable,
        extract.price_debug_note,
        extract.listing_url,
    )
    _validate_bama_extract_for_persist(extract)

    listing_id: int | None = None
    scraped_at_snapshot = None

    with SessionLocal() as db:
        try:
            listing_row = _upsert_bama_scraped_listing(db, extract)
            db.flush()
            listing_id = getattr(listing_row, "id", None)
            LOGGER.info(
                "bama_ingestion_task flushed scraped_listings pending commit pk=%s external_id=%r "
                "(scraped_at NOT set in ORM — DB server_default)",
                listing_id,
                extract.external_id,
            )

            db.commit()
            db.refresh(listing_row)
            listing_id = listing_row.id
            scraped_at_snapshot = getattr(listing_row, "scraped_at", None)
            LOGGER.info(
                "bama_ingestion_task SUCCESS persisted scraped_listing_id=%s external_id=%r "
                "scraped_at=%s INPUT_URL=%s",
                listing_id,
                extract.external_id,
                scraped_at_snapshot,
                input_url,
            )
        except OperationalError as exc:
            LOGGER.warning(
                "bama_ingestion_task RETRY_TRIGGER DB OperationalError external_id=%r INPUT_URL=%s task_id=%s detail=%s",
                extract.external_id,
                input_url,
                task_id,
                exc.orig if hasattr(exc, "orig") else exc,
                exc_info=True,
            )
            db.rollback()
            raise
        except IntegrityError as exc:
            LOGGER.exception(
                "bama_ingestion_task IntegrityError on flush/commit (e.g. unique uq_scraped_listings_source_external_id "
                "or FK); rolled back. external_id=%r url=%s detail=%s",
                extract.external_id,
                extract.listing_url,
                exc.orig if hasattr(exc, "orig") else exc,
            )
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            LOGGER.exception(
                "bama_ingestion_task SQLAlchemyError on flush/commit; rolled back. external_id=%r url=%s",
                extract.external_id,
                extract.listing_url,
            )
            db.rollback()
            raise

    return {
        "status": "ok",
        "external_id": extract.external_id,
        "scraped_listing_id": listing_id,
        "url": extract.listing_url,
        "scraped_at": scraped_at_snapshot.isoformat() if scraped_at_snapshot else None,
    }


@celery_app.task(name="tasks.bama_ingestion_tick")
def bama_ingestion_tick_task() -> dict[str, object]:
    """Beat hook: dequeue (Redis LPOP) or fall back to BAMA_INGESTION_URL, then enqueue bama_ingestion_task."""
    url = _pop_next_bama_url()
    if not url:
        LOGGER.info("bama_ingestion_tick skipped — no URL in Redis queue and BAMA_INGESTION_URL unset.")
        return {"status": "skipped", "reason": "no_url"}

    LOGGER.info("bama_ingestion_tick enqueued URL: %s", url)
    bama_ingestion_task.delay(url)
    return {"status": "enqueued", "url": url}


@celery_app.task(
    bind=True,
    autoretry_for=_bama_retry_exceptions(),
    retry_kwargs={"max_retries": 3, "countdown": 45},
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    name="tasks.hm_ingestion_task",
)
def hm_ingestion_task(self, url: str) -> dict[str, object]:
    """Fetch one HM listing URL and upsert scraped_listings (source=hm)."""
    input_url = (url or "").strip()
    task_id = getattr(self.request, "id", None)
    LOGGER.info("hm_ingestion_task INPUT_URL=%s task_id=%s", input_url, task_id)

    if not input_url:
        LOGGER.error("hm_ingestion_task aborted: empty INPUT_URL")
        raise ValueError("hm_ingestion_task requires a non-empty url")

    try:
        extract = fetch_and_parse_hm_listing(input_url)
    except requests.HTTPError as exc:
        _handle_status_circuit_breaker(self, input_url=input_url, exc=exc, task_name="hm_ingestion_task")
        raise
    except _bama_retry_exceptions() as exc:
        LOGGER.warning(
            "hm_ingestion_task RETRY_TRIGGER kind=%s INPUT_URL=%s task_id=%s detail=%s",
            type(exc).__name__,
            input_url,
            task_id,
            exc,
            exc_info=True,
        )
        raise

    LOGGER.info(
        "hm_ingestion_task EXTRACT_SUMMARY external_id=%r price=%s mileage=%s brand=%r model=%r year=%s body_condition=%r created_at_source=%r url=%s",
        extract.external_id,
        extract.price,
        extract.mileage,
        extract.brand,
        extract.model,
        extract.year,
        extract.body_condition,
        extract.created_at_source,
        extract.listing_url,
    )
    _validate_hm_extract_for_persist(extract)

    listing_id: int | None = None
    scraped_at_snapshot = None

    with SessionLocal() as db:
        try:
            listing_row = _upsert_hm_scraped_listing(db, extract)
            db.flush()
            listing_id = getattr(listing_row, "id", None)

            db.commit()
            db.refresh(listing_row)
            listing_id = listing_row.id
            scraped_at_snapshot = getattr(listing_row, "scraped_at", None)
            LOGGER.info(
                "hm_ingestion_task SUCCESS persisted scraped_listing_id=%s external_id=%r parse_error=%s scraped_at=%s INPUT_URL=%s",
                listing_id,
                extract.external_id,
                _hm_has_parse_error(extract),
                scraped_at_snapshot,
                input_url,
            )
        except OperationalError as exc:
            LOGGER.warning(
                "hm_ingestion_task RETRY_TRIGGER DB OperationalError external_id=%r INPUT_URL=%s task_id=%s detail=%s",
                extract.external_id,
                input_url,
                task_id,
                exc.orig if hasattr(exc, "orig") else exc,
                exc_info=True,
            )
            db.rollback()
            raise
        except IntegrityError as exc:
            LOGGER.exception(
                "hm_ingestion_task IntegrityError on flush/commit external_id=%r url=%s detail=%s",
                extract.external_id,
                extract.listing_url,
                exc.orig if hasattr(exc, "orig") else exc,
            )
            db.rollback()
            raise
        except SQLAlchemyError:
            LOGGER.exception(
                "hm_ingestion_task SQLAlchemyError on flush/commit; rolled back. external_id=%r url=%s",
                extract.external_id,
                extract.listing_url,
            )
            db.rollback()
            raise

    return {
        "status": "ok",
        "external_id": extract.external_id,
        "scraped_listing_id": listing_id,
        "parse_error": _hm_has_parse_error(extract),
        "url": extract.listing_url,
        "scraped_at": scraped_at_snapshot.isoformat() if scraped_at_snapshot else None,
    }


@celery_app.task(name="tasks.hm_ingestion_tick")
def hm_ingestion_tick_task() -> dict[str, object]:
    """Beat hook: dequeue HM URL from Redis or HM_INGESTION_URL and enqueue hm_ingestion_task."""
    url = _pop_next_hm_url()
    if not url:
        LOGGER.info("hm_ingestion_tick skipped — no URL in Redis queue and HM_INGESTION_URL unset.")
        return {"status": "skipped", "reason": "no_url"}

    LOGGER.info("hm_ingestion_tick enqueued URL: %s", url)
    hm_ingestion_task.delay(url)
    return {"status": "enqueued", "url": url}


@celery_app.task(
    bind=True,
    autoretry_for=_bama_retry_exceptions(),
    retry_kwargs={"max_retries": 3, "countdown": 45},
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    name="tasks.kn_ingestion_task",
)
def kn_ingestion_task(self, url: str) -> dict[str, object]:
    """Fetch one Karnameh listing URL and upsert scraped_listings (source=karnameh)."""
    input_url = (url or "").strip()
    task_id = getattr(self.request, "id", None)
    LOGGER.info("kn_ingestion_task INPUT_URL=%s task_id=%s", input_url, task_id)

    if not input_url:
        LOGGER.error("kn_ingestion_task aborted: empty INPUT_URL")
        raise ValueError("kn_ingestion_task requires a non-empty url")

    try:
        extract = fetch_and_parse_kn_listing(input_url)
    except requests.HTTPError as exc:
        _handle_status_circuit_breaker(self, input_url=input_url, exc=exc, task_name="kn_ingestion_task")
        raise
    except _bama_retry_exceptions() as exc:
        LOGGER.warning(
            "kn_ingestion_task RETRY_TRIGGER kind=%s INPUT_URL=%s task_id=%s detail=%s",
            type(exc).__name__,
            input_url,
            task_id,
            exc,
            exc_info=True,
        )
        raise

    LOGGER.info(
        "kn_ingestion_task EXTRACT_SUMMARY external_id=%r price=%s mileage=%s brand=%r model=%r trim=%r body_condition=%r created_at_source=%r url=%s",
        extract.external_id,
        extract.price,
        extract.mileage,
        extract.brand,
        extract.model,
        extract.trim,
        extract.body_condition,
        extract.created_at_source,
        extract.listing_url,
    )
    _validate_kn_extract_with_content_guard(extract)

    listing_id: int | None = None
    scraped_at_snapshot = None

    with SessionLocal() as db:
        try:
            listing_row = _upsert_kn_scraped_listing(db, extract)
            db.flush()
            listing_id = getattr(listing_row, "id", None)

            db.commit()
            db.refresh(listing_row)
            listing_id = listing_row.id
            scraped_at_snapshot = getattr(listing_row, "scraped_at", None)
            LOGGER.info(
                "kn_ingestion_task SUCCESS persisted scraped_listing_id=%s external_id=%r parse_error=%s scraped_at=%s INPUT_URL=%s",
                listing_id,
                extract.external_id,
                _kn_has_parse_error(extract),
                scraped_at_snapshot,
                input_url,
            )
        except OperationalError as exc:
            LOGGER.warning(
                "kn_ingestion_task RETRY_TRIGGER DB OperationalError external_id=%r INPUT_URL=%s task_id=%s detail=%s",
                extract.external_id,
                input_url,
                task_id,
                exc.orig if hasattr(exc, "orig") else exc,
                exc_info=True,
            )
            db.rollback()
            raise
        except IntegrityError as exc:
            LOGGER.exception(
                "kn_ingestion_task IntegrityError on flush/commit external_id=%r url=%s detail=%s",
                extract.external_id,
                extract.listing_url,
                exc.orig if hasattr(exc, "orig") else exc,
            )
            db.rollback()
            raise
        except SQLAlchemyError:
            LOGGER.exception(
                "kn_ingestion_task SQLAlchemyError on flush/commit; rolled back. external_id=%r url=%s",
                extract.external_id,
                extract.listing_url,
            )
            db.rollback()
            raise

    return {
        "status": "ok",
        "external_id": extract.external_id,
        "scraped_listing_id": listing_id,
        "parse_error": _kn_has_parse_error(extract),
        "url": extract.listing_url,
        "scraped_at": scraped_at_snapshot.isoformat() if scraped_at_snapshot else None,
    }


@celery_app.task(name="tasks.kn_ingestion_tick")
def kn_ingestion_tick_task() -> dict[str, object]:
    """Beat hook: dequeue KN URL from Redis or KN_INGESTION_URL and enqueue kn_ingestion_task."""
    url = _pop_next_kn_url()
    if not url:
        LOGGER.info("kn_ingestion_tick skipped — no URL in Redis queue and KN_INGESTION_URL unset.")
        return {"status": "skipped", "reason": "no_url"}

    LOGGER.info("kn_ingestion_tick enqueued URL: %s", url)
    kn_ingestion_task.delay(url)
    return {"status": "enqueued", "url": url}

