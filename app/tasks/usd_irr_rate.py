"""Celery entrypoint for free-market USD/IRR rate (TGJU)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.celery_app import celery_app

LOGGER = logging.getLogger(__name__)


@celery_app.task(name="tasks.usd_irr_rate_scrape", soft_time_limit=60, time_limit=90)
def usd_irr_rate_scrape() -> dict[str, Any]:
    """
    Fetch TGJU ``price_dollar_rl`` (دلار آزاد) and upsert ``usd_irr_rate`` for Tehran today.

    Beat: 11:00 Asia/Tehran (07:30 UTC) and 18:00 Asia/Tehran (14:30 UTC).
    Morning run must finish by 11:10 so Seenad can SELECT at 11:15.
    """

    from src.scraper.usd_irr_rate_service import run_usd_irr_rate_pipeline

    dry_run = os.getenv("USD_IRR_RATE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    started = datetime.now(timezone.utc)
    LOGGER.info("tasks.usd_irr_rate_scrape start dry_run=%s started_at=%s", dry_run, started.isoformat())
    summary = run_usd_irr_rate_pipeline(dry_run=dry_run)
    payload = summary.as_celery_dict()
    LOGGER.info(
        "USD_IRR_RATE_SCRAPE_DONE success=%s as_of_date=%s action=%s insert=%s update=%s "
        "carried_forward=%s rate_irr=%s rate_toman=%s row_id=%s elapsed_s=%s error=%s",
        summary.success,
        summary.as_of_date,
        summary.action,
        1 if summary.action == "insert" else 0,
        1 if summary.action == "update" else 0,
        summary.carried_forward,
        summary.rate_irr,
        summary.rate_toman,
        summary.row_id,
        summary.elapsed_s,
        summary.error,
    )
    if not summary.success:
        LOGGER.error(
            "usd_irr_rate today's row NOT created as_of_date=%s error=%s",
            summary.as_of_date,
            summary.error,
        )
    LOGGER.info("tasks.usd_irr_rate_scrape done %s", payload)
    return payload
