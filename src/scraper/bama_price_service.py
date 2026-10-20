"""
Bama.ir zero-price (catalogue day price) raw ingestion via official CAD JSON APIs.

Feeds used (same as the Nuxt frontend on https://bama.ir/price):

- ``GET /cad/api/price/hierarchy?pageIndex=&pageSize=`` — full catalogue hierarchy
  (all brands/models/trims), not “popular-only” UX lists. Pagination walks every page until
  the API returns no data or fewer than ``page_size`` groups (unless ``max_hierarchy_pages`` caps it).

Persist **every** hierarchy line as string fields + JSON in ``car_zero_price_scrape_snapshot``
under a fresh ``batch_id``. Entity matching and ``car_zero_price`` upserts belong on the Main Server.

"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Iterator, Mapping

import requests
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import CarZeroPriceScrapeSnapshot
from src.scraper.zero_price_batch_registry import (
    ZeroPriceBatchSource,
    abort_zero_price_batch,
    begin_zero_price_batch,
    complete_zero_price_batch,
)

LOGGER: Final = logging.getLogger(__name__)

BAMA_BASE_URL: Final[str] = "https://bama.ir"
HIERARCHY_PATH: Final[str] = "/cad/api/price/hierarchy"
DEFAULT_PAGE_SIZE: Final[int] = 75
DEFAULT_PAGE_SIZE_MAX: Final[int] = 500
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 45.0
DEFAULT_HTTP_MAX_ATTEMPTS: Final[int] = 5
DEFAULT_HTTP_BACKOFF_BASE_S: Final[float] = 2.0
DEFAULT_HTTP_BACKOFF_CAP_S: Final[float] = 30.0
RAW_SNAPSHOT_DB_FLUSH_INTERVAL: Final[int] = 400
RETRYABLE_HTTP_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BamaHierarchyTransientError(Exception):
    """Timeouts, gateway flaps, or empty catalogue worth retrying at Celery level."""


def generate_scrape_batch_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class BamaHierarchyItem:
    """One trim line from the hierarchy API ``items`` array."""

    brand_slug: str
    brand_fa: str
    model_slug: str
    model_fa: str
    trim_slug: str
    trim_fa: str
    price_api_raw: int
    price_irr: int
    model_year: int | None
    price_provider: str | None
    bama_car_price_id: int | None
    raw: Mapping[str, Any]
    brand_name_raw: str
    brand_slug_raw: str
    model_name_raw: str
    model_slug_raw: str
    trim_name_raw: str
    trim_slug_raw: str
    model_year_raw: str
    price_raw: str



@dataclass(frozen=True, slots=True)
class BamaZeroPriceRunSummary:
    pages_fetched: int
    items_seen: int
    items_with_price: int
    matched: int
    upserted: int
    skipped_no_entity: int
    rejected_low_score: int
    errors_http: int
    raw_rows_persisted: int
    batch_id: str | None

    def as_celery_dict(self) -> dict[str, Any]:
        return {
            "pages_fetched": self.pages_fetched,
            "items_seen": self.items_seen,
            "items_with_price": self.items_with_price,
            "matched": self.matched,
            "upserted": self.upserted,
            "skipped_no_entity": self.skipped_no_entity,
            "rejected_low_score": self.rejected_low_score,
            "errors_http": self.errors_http,
            "raw_rows_persisted": self.raw_rows_persisted,
            "batch_id": self.batch_id,
        }


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
        }
    )
    return session


def _backoff_seconds(attempt: int, *, base: float, cap: float) -> float:
    wait = min(cap, base * (2 ** (attempt - 1)))
    return wait + random.uniform(0.0, wait * 0.25)


def _retry_after_seconds(response: requests.Response, fallback: float) -> float:
    raw = response.headers.get("Retry-After")
    if not raw:
        return fallback
    try:
        return max(fallback, float(raw))
    except ValueError:
        return fallback


def fetch_hierarchy_page(
    session: requests.Session,
    *,
    url: str,
    page_index: int,
    page_size: int,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_attempts: int | None = None,
    backoff_base: float | None = None,
    backoff_cap: float | None = None,
) -> list[Any]:
    """GET one hierarchy page. Retry timeouts and gateway/CDN errors; fail fast on 4xx like 403."""

    attempts = max_attempts if max_attempts is not None else _env_int(
        "BAMA_ZERO_PRICE_HTTP_MAX_ATTEMPTS", DEFAULT_HTTP_MAX_ATTEMPTS, minimum=1
    )
    base = backoff_base if backoff_base is not None else _env_float(
        "BAMA_ZERO_PRICE_HTTP_BACKOFF_BASE_S", DEFAULT_HTTP_BACKOFF_BASE_S, minimum=0.1
    )
    cap = backoff_cap if backoff_cap is not None else _env_float(
        "BAMA_ZERO_PRICE_HTTP_BACKOFF_CAP_S", DEFAULT_HTTP_BACKOFF_CAP_S, minimum=1.0
    )
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                url,
                params={"pageIndex": page_index, "pageSize": page_size},
                timeout=timeout_seconds,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            wait = _backoff_seconds(attempt, base=base, cap=cap)
            LOGGER.warning(
                "bama_price hierarchy page=%s network error attempt=%s/%s kind=%s detail=%s — sleep %.1fs",
                page_index,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                wait,
            )
            time.sleep(wait)
            continue

        if response.status_code in RETRYABLE_HTTP_STATUS:
            last_exc = requests.HTTPError(
                f"{response.status_code} Server Error: {response.reason} for url: {response.url}",
                response=response,
            )
            if attempt >= attempts:
                break
            wait = _retry_after_seconds(
                response, _backoff_seconds(attempt, base=base, cap=cap)
            )
            LOGGER.warning(
                "bama_price hierarchy page=%s HTTP %s attempt=%s/%s — sleep %.1fs",
                page_index,
                response.status_code,
                attempt,
                attempts,
                wait,
            )
            time.sleep(wait)
            continue

        response.raise_for_status()
        payload: Mapping[str, Any] = response.json()
        blocks = payload.get("data") or []
        if not isinstance(blocks, list):
            blocks = []
        if attempt > 1:
            LOGGER.info(
                "bama_price hierarchy page=%s recovered after attempt=%s/%s",
                page_index,
                attempt,
                attempts,
            )
        return blocks

    assert last_exc is not None
    LOGGER.error(
        "bama_price hierarchy page=%s exhausted attempts=%s last_error=%s",
        page_index,
        attempts,
        last_exc,
    )
    raise last_exc


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _api_raw_str(value: Any) -> str:
    """Preserve API scalars as strings for fuzzy pre-mapping (no parsing / locale normalization)."""

    if value is None:
        return ""
    return str(value)


def _coerce_price_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_bama_hierarchy_price_to_irr(raw_price_toman: int) -> int:
    """
    Convert a Bama hierarchy ``price`` integer to Iranian Rial for ``currency='IRR'``.

    The CAD hierarchy API always returns catalogue prices in Toman (no unit field on items).
    """

    if raw_price_toman <= 0:
        return 0
    return raw_price_toman * 10


def _parse_hierarchy_item(
    brand_bundle: Mapping[str, Any],
    item: Mapping[str, Any],
) -> BamaHierarchyItem:
    brand_slug_val = _api_raw_str(item.get("brand") or brand_bundle.get("brand"))
    brand_fa_val = _api_raw_str(item.get("brand_fa") or brand_bundle.get("brand_fa"))
    model_slug_val = _api_raw_str(item.get("model"))
    model_fa_val = _api_raw_str(item.get("model_fa"))
    trim_slug_val = _api_raw_str(item.get("trim"))
    trim_fa_val = _api_raw_str(item.get("trim_fa"))
    price_raw = _api_raw_str(item.get("price"))
    model_year_raw = _api_raw_str(item.get("model_year"))
    price_provider = item.get("price_provider") if item.get("price_provider") else None
    price_api_raw = _coerce_price_int(item.get("price"))
    price_irr = normalize_bama_hierarchy_price_to_irr(price_api_raw)
    return BamaHierarchyItem(
        brand_slug=brand_slug_val,
        brand_fa=brand_fa_val,
        model_slug=model_slug_val,
        model_fa=model_fa_val,
        trim_slug=trim_slug_val,
        trim_fa=trim_fa_val,
        price_api_raw=price_api_raw,
        price_irr=price_irr,
        model_year=_optional_int(item.get("model_year")),
        price_provider=price_provider,
        bama_car_price_id=_optional_int(item.get("car_price_id")),
        raw=item,
        brand_name_raw=brand_fa_val,
        brand_slug_raw=brand_slug_val,
        model_name_raw=model_fa_val,
        model_slug_raw=model_slug_val,
        trim_name_raw=trim_fa_val,
        trim_slug_raw=trim_slug_val,
        model_year_raw=model_year_raw,
        price_raw=price_raw,
    )


def iterate_hierarchy_items(
    session: requests.Session,
    *,
    base_url: str = BAMA_BASE_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_S,
    inter_page_delay_seconds: tuple[float, float] = (0.4, 1.2),
    max_hierarchy_pages: int | None = None,
) -> Iterator[tuple[int, BamaHierarchyItem]]:
    """Yield ``(page_index, item)`` across all hierarchy pages (for bookkeeping).

    Stops when the API returns no ``data``, or when the number of group blocks is below
    ``page_size`` (last page). Set ``max_hierarchy_pages`` only for smoke tests — full
    catalogue crawls should leave it ``None``.
    """

    url = base_url.rstrip("/") + HIERARCHY_PATH
    page_index = 0
    while True:
        if max_hierarchy_pages is not None and page_index >= max_hierarchy_pages:
            LOGGER.warning(
                "bama_price hierarchy early stop max_hierarchy_pages=%s at page_index=%s",
                max_hierarchy_pages,
                page_index,
            )
            return

        time.sleep(random.uniform(*inter_page_delay_seconds))
        blocks = fetch_hierarchy_page(
            session,
            url=url,
            page_index=page_index,
            page_size=page_size,
            timeout_seconds=timeout_seconds,
        )
        item_count = 0
        for group in blocks:
            if not isinstance(group, Mapping):
                continue
            for raw_item in group.get("items") or []:
                if isinstance(raw_item, Mapping):
                    item_count += 1

        LOGGER.info(
            "bama_price hierarchy page=%s pageSize(groups)=%s groups=%s trim_rows=%s",
            page_index,
            page_size,
            len(blocks),
            item_count,
        )

        if not blocks:
            return

        for group in blocks:
            if not isinstance(group, Mapping):
                continue
            for raw_item in group.get("items") or []:
                if not isinstance(raw_item, Mapping):
                    continue
                yield page_index, _parse_hierarchy_item(group, raw_item)

        if len(blocks) < page_size:
            return

        page_index += 1


def insert_scrape_snapshot_row(db: Session, *, batch_id: str, page_index: int, row: BamaHierarchyItem) -> bool:
    raw_json: dict[str, Any] | None
    try:
        raw_json = dict(row.raw)
    except (TypeError, ValueError):
        raw_json = None

    stmt = pg_insert(CarZeroPriceScrapeSnapshot).values(
        batch_id=batch_id,
        hierarchy_page_index=page_index,
        brand_name_raw=row.brand_name_raw,
        brand_slug_raw=row.brand_slug_raw,
        model_name_raw=row.model_name_raw,
        model_slug_raw=row.model_slug_raw,
        trim_name_raw=row.trim_name_raw,
        trim_slug_raw=row.trim_slug_raw,
        model_year_raw=row.model_year_raw,
        price_raw=row.price_raw,
        price_provider=row.price_provider,
        bama_car_price_id=row.bama_car_price_id,
        raw_json=raw_json,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            CarZeroPriceScrapeSnapshot.batch_id,
            CarZeroPriceScrapeSnapshot.bama_car_price_id,
        ],
        index_where=text("bama_car_price_id IS NOT NULL"),
    )
    result = db.execute(stmt)
    return result.rowcount != 0


def run_bama_zero_price_pipeline(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    dry_run: bool = False,
    capture_raw_rows: bool = True,
    batch_id: str | None = None,
    max_hierarchy_pages: int | None = None,
) -> BamaZeroPriceRunSummary:
    """
    Download the full Bama price hierarchy and persist every line to
    ``car_zero_price_scrape_snapshot`` (raw ingestion only).

    Entity matching and ``car_zero_price`` upserts are disabled on the Scraper Server;
    the Main Server consumes snapshot batches for mapping.
    """

    # Raw snapshot capture is always on for this pipeline (Phase A scraper purification).
    capture_raw_rows = True
    logical_batch: str | None = (batch_id or "").strip() or generate_scrape_batch_id()
    persist_batch: str | None = None if dry_run else logical_batch

    LOGGER.info(
        "bama_price pipeline start dry_run=%s page_size=%s capture_raw_rows=%s batch_id=%s max_pages=%s endpoint=%s",
        dry_run,
        page_size,
        capture_raw_rows,
        logical_batch,
        max_hierarchy_pages,
        HIERARCHY_PATH,
    )
    http = _http_session()
    items_seen = 0
    errors_http = 0
    raw_rows_persisted = 0

    page_indexes: set[int] = set()
    with SessionLocal() as db:
        try:
            if persist_batch:
                begin_zero_price_batch(
                    db,
                    source=ZeroPriceBatchSource.BAMA,
                    batch_id=persist_batch,
                )
                db.commit()
            for page_idx, item in iterate_hierarchy_items(
                http,
                page_size=page_size,
                max_hierarchy_pages=max_hierarchy_pages,
            ):
                page_indexes.add(page_idx)
                items_seen += 1
                if persist_batch:
                    inserted_snapshot = insert_scrape_snapshot_row(
                        db,
                        batch_id=persist_batch,
                        page_index=page_idx,
                        row=item,
                    )
                    if inserted_snapshot:
                        raw_rows_persisted += 1
                        if raw_rows_persisted % RAW_SNAPSHOT_DB_FLUSH_INTERVAL == 0:
                            db.commit()
                            LOGGER.info(
                                "bama_price raw snapshot flushed rows=%s batch_id=%s",
                                raw_rows_persisted,
                                persist_batch,
                            )
            if persist_batch:
                db.commit()
                if raw_rows_persisted > 0:
                    complete_zero_price_batch(
                        db,
                        source=ZeroPriceBatchSource.BAMA,
                        batch_id=persist_batch,
                        raw_row_count=raw_rows_persisted,
                    )
                    db.commit()
        except (requests.RequestException, BamaHierarchyTransientError) as exc:
            errors_http += 1
            LOGGER.exception("bama_price HTTP failure: %s", exc)
            db.rollback()
            if persist_batch:
                with SessionLocal() as cleanup_db:
                    abort_zero_price_batch(
                        cleanup_db,
                        source=ZeroPriceBatchSource.BAMA,
                        batch_id=persist_batch,
                    )
                    cleanup_db.commit()
            raise
        except Exception:
            db.rollback()
            if persist_batch:
                with SessionLocal() as cleanup_db:
                    abort_zero_price_batch(
                        cleanup_db,
                        source=ZeroPriceBatchSource.BAMA,
                        batch_id=persist_batch,
                    )
                    cleanup_db.commit()
            raise

    if persist_batch and raw_rows_persisted <= 0:
        with SessionLocal() as cleanup_db:
            abort_zero_price_batch(
                cleanup_db,
                source=ZeroPriceBatchSource.BAMA,
                batch_id=persist_batch,
            )
            cleanup_db.commit()
        raise BamaHierarchyTransientError(
            f"bama_price empty snapshot batch_id={logical_batch} pages={len(page_indexes)}"
        )

    summary = BamaZeroPriceRunSummary(
        pages_fetched=len(page_indexes),
        items_seen=items_seen,
        items_with_price=0,
        matched=0,
        upserted=0,
        skipped_no_entity=0,
        rejected_low_score=0,
        errors_http=errors_http,
        raw_rows_persisted=raw_rows_persisted,
        batch_id=logical_batch,
    )
    LOGGER.info(
        "bama_price run summary | hierarchy_rows=%s raw_snapshot_rows=%s pages=%s batch_id=%s",
        summary.items_seen,
        summary.raw_rows_persisted,
        summary.pages_fetched,
        summary.batch_id,
    )
    LOGGER.info("bama_price pipeline done %s", summary.as_celery_dict())
    return summary
