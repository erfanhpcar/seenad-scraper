"""Celery entrypoints for Bama zero-kilometre price scraping (bama.ir/price)."""

from __future__ import annotations

import logging
import os
from typing import Any

from app.celery_app import celery_app

LOGGER = logging.getLogger(__name__)


@celery_app.task(name="tasks.bama_zero_price_scrape")
def bama_zero_price_scrape() -> dict[str, Any]:
    """
    Full hierarchy fetch, snapshot raw rows, fuzzy match to ``car_entities``, upsert ``car_zero_price``.

    Bounded by SoftTimeLimit / TimeLimit on the worker.
    """

    from src.scraper.bama_price_service import (
        DEFAULT_PAGE_SIZE,
        DEFAULT_PAGE_SIZE_MAX,
        run_bama_zero_price_pipeline,
    )

    # Large page size minimizes HTTP round-trips for full catalogue depth (all hierarchy pages).
    page_size = DEFAULT_PAGE_SIZE_MAX
    raw = os.getenv("BAMA_ZERO_PRICE_PAGE_SIZE", "").strip()
    if raw.isdigit():
        page_size = max(10, min(int(raw), DEFAULT_PAGE_SIZE_MAX))

    dry_run = os.getenv("BAMA_ZERO_PRICE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    capture_raw = os.getenv("BAMA_ZERO_PRICE_CAPTURE_RAW", "1").strip().lower() not in {"0", "false", "no"}
    full_crawl = os.getenv("BAMA_ZERO_PRICE_FULL_CRAWL", "").strip().lower() in {"1", "true", "yes"}
    if full_crawl:
        page_size = DEFAULT_PAGE_SIZE_MAX
        capture_raw = True
    max_pages_env = os.getenv("BAMA_ZERO_PRICE_MAX_PAGES", "").strip()
    max_hierarchy_pages: int | None = None
    if max_pages_env.isdigit():
        max_hierarchy_pages = int(max_pages_env)

    LOGGER.info(
        "tasks.bama_zero_price_scrape start page_size=%s dry_run=%s capture_raw=%s max_pages=%s",
        page_size,
        dry_run,
        capture_raw,
        max_hierarchy_pages,
    )
    summary = run_bama_zero_price_pipeline(
        page_size=page_size,
        dry_run=dry_run,
        capture_raw_rows=capture_raw,
        max_hierarchy_pages=max_hierarchy_pages,
    )
    payload = summary.as_celery_dict()
    LOGGER.info(
        "BAMA_ZERO_PRICE_SCRAPE_SUCCESS success=true batch_id=%s raw_rows_persisted=%s upserted=%s pages=%s",
        summary.batch_id,
        summary.raw_rows_persisted,
        summary.upserted,
        summary.pages_fetched,
    )
    LOGGER.info("tasks.bama_zero_price_scrape done %s", payload)
    return payload
