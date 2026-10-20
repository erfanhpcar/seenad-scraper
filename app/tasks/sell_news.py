"""Celery entrypoint for sell-condition / بخشنامه crawl (11:00 and 16:00 Asia/Tehran)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.celery_app import celery_app

LOGGER = logging.getLogger(__name__)


@celery_app.task(name="tasks.sell_news_scrape", soft_time_limit=480, time_limit=540)
def sell_news_scrape() -> dict[str, Any]:
    """
    Crawl khodro45 / khodrobank / car.ir / autonovin / megfa sell-news and upsert Postgres.

    Scheduled 11:00 Asia/Tehran (07:30 UTC) and 16:00 Asia/Tehran (12:30 UTC).
    Morning run must finish by 11:10 so Seenad can read scrape_batch_id for
    today at 11:15. Does not call any Seenad API.
    """

    from src.scraper.sell_news.run import run_sell_news

    dry_run = os.getenv("SELL_NEWS_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    backfill = os.getenv("SELL_NEWS_BACKFILL", "").strip().lower() in {"1", "true", "yes"}
    LOGGER.info(
        "tasks.sell_news_scrape start dry_run=%s backfill=%s started_at=%s",
        dry_run,
        backfill,
        datetime.now(timezone.utc).isoformat(),
    )
    summary = run_sell_news(dry_run=dry_run, backfill=backfill)
    totals = summary.get("totals") or {}
    LOGGER.info(
        "SELL_NEWS_SCRAPE_DONE batch=%s started_at=%s finished_at=%s elapsed_s=%s "
        "found=%s insert=%s update=%s skip=%s error=%s",
        summary.get("scrape_batch_id"),
        summary.get("started_at"),
        summary.get("finished_at"),
        summary.get("elapsed_s"),
        totals.get("found"),
        totals.get("insert"),
        totals.get("update"),
        totals.get("skip"),
        totals.get("error"),
    )
    return {
        "scrape_batch_id": summary.get("scrape_batch_id"),
        "dry_run": dry_run,
        "started_at": summary.get("started_at"),
        "finished_at": summary.get("finished_at"),
        "elapsed_s": summary.get("elapsed_s"),
        "totals": totals,
        "sources": [
            {
                "source": row.get("source"),
                "found": row.get("found"),
                "insert": row.get("insert"),
                "update": row.get("update"),
                "skip": row.get("skip"),
                "error": row.get("error"),
                "timeout": row.get("timeout"),
                "coverage": row.get("coverage"),
                "notes": row.get("notes"),
            }
            for row in summary.get("sources", [])
        ],
    }
