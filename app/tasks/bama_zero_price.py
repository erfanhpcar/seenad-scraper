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
    Full hierarchy fetch, fuzzy match to ``car_entities``, upsert ``car_zero_price``.

    Bounded by SoftTimeLimit / TimeLimit on the worker.
    """

    from src.scraper.bama_price_service import DEFAULT_PAGE_SIZE, run_bama_zero_price_pipeline

    page_size = DEFAULT_PAGE_SIZE
    raw = os.getenv("BAMA_ZERO_PRICE_PAGE_SIZE", "").strip()
    if raw.isdigit():
        page_size = max(10, min(int(raw), 200))

    dry_run = os.getenv("BAMA_ZERO_PRICE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    LOGGER.info("tasks.bama_zero_price_scrape start page_size=%s dry_run=%s", page_size, dry_run)
    summary = run_bama_zero_price_pipeline(page_size=page_size, dry_run=dry_run)
    payload = summary.as_celery_dict()
    LOGGER.info("tasks.bama_zero_price_scrape done %s", payload)
    return payload
