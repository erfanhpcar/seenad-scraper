"""Celery entrypoints for Bama zero-kilometre price scraping (bama.ir/price)."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from celery.exceptions import Ignore

from app.celery_app import celery_app
from src.scraper.bama_price_service import BamaHierarchyTransientError

LOGGER = logging.getLogger(__name__)

_RETRYABLE = (
    requests.RequestException,
    BamaHierarchyTransientError,
    OSError,
    TimeoutError,
    ConnectionError,
)


def _zero_price_retry_exceptions() -> tuple[type[BaseException], ...]:
    return _RETRYABLE


@celery_app.task(
    bind=True,
    autoretry_for=_zero_price_retry_exceptions(),
    retry_kwargs={"max_retries": 3},
    # Celery overwrites countdown whenever retry_backoff is truthy. True→factor 1
    # (0s/1s/4s with jitter). Use 60s factor so delays are 60 / 120 / 240.
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=False,
    name="tasks.bama_zero_price_scrape",
)
def bama_zero_price_scrape(self) -> dict[str, Any]:
    """
    Full hierarchy fetch; persist raw rows to ``car_zero_price_scrape_snapshot`` only.

    Per-page HTTP retries live in ``fetch_hierarchy_page``. This Celery autoretry is the
    second line of defence when Bama stays down for the whole page-attempt budget.
    Bounded by SoftTimeLimit / TimeLimit on the worker.
    """

    from src.scraper.bama_price_service import (
        DEFAULT_PAGE_SIZE_MAX,
        run_bama_zero_price_pipeline,
    )

    # Large page size minimizes HTTP round-trips for full catalogue depth (all hierarchy pages).
    page_size = DEFAULT_PAGE_SIZE_MAX
    raw = os.getenv("BAMA_ZERO_PRICE_PAGE_SIZE", "").strip()
    if raw.isdigit():
        page_size = max(10, min(int(raw), DEFAULT_PAGE_SIZE_MAX))

    dry_run = os.getenv("BAMA_ZERO_PRICE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    capture_raw = True
    full_crawl = os.getenv("BAMA_ZERO_PRICE_FULL_CRAWL", "").strip().lower() in {"1", "true", "yes"}
    if full_crawl:
        page_size = DEFAULT_PAGE_SIZE_MAX
    max_pages_env = os.getenv("BAMA_ZERO_PRICE_MAX_PAGES", "").strip()
    max_hierarchy_pages: int | None = None
    if max_pages_env.isdigit():
        max_hierarchy_pages = int(max_pages_env)

    retries = getattr(self.request, "retries", 0)
    task_id = getattr(self.request, "id", None)
    LOGGER.info(
        "tasks.bama_zero_price_scrape start page_size=%s dry_run=%s capture_raw=%s "
        "max_pages=%s retry=%s/%s task_id=%s",
        page_size,
        dry_run,
        capture_raw,
        max_hierarchy_pages,
        retries,
        self.max_retries,
        task_id,
    )
    try:
        summary = run_bama_zero_price_pipeline(
            page_size=page_size,
            dry_run=dry_run,
            capture_raw_rows=capture_raw,
            max_hierarchy_pages=max_hierarchy_pages,
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 403:
            LOGGER.critical(
                "CRITICAL: IP PROBABLY BANNED site=bama_zero_price status=403 task_id=%s",
                task_id,
            )
            raise Ignore() from exc
        LOGGER.warning(
            "bama_zero_price_scrape RETRY_TRIGGER kind=%s status=%s retry=%s/%s task_id=%s detail=%s",
            type(exc).__name__,
            status_code,
            retries,
            self.max_retries,
            task_id,
            exc,
        )
        raise
    except _RETRYABLE as exc:
        LOGGER.warning(
            "bama_zero_price_scrape RETRY_TRIGGER kind=%s retry=%s/%s task_id=%s detail=%s",
            type(exc).__name__,
            retries,
            self.max_retries,
            task_id,
            exc,
        )
        raise

    payload = summary.as_celery_dict()
    LOGGER.info(
        "BAMA_ZERO_PRICE_SCRAPE_SUCCESS success=true batch_id=%s raw_rows_persisted=%s upserted=%s pages=%s retry=%s",
        summary.batch_id,
        summary.raw_rows_persisted,
        summary.upserted,
        summary.pages_fetched,
        retries,
    )
    LOGGER.info("tasks.bama_zero_price_scrape done %s", payload)
    return payload
