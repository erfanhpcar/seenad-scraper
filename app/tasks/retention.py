"""Nightly retention: delete rows older than the configured retention window."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.celery_app import celery_app
from app.database import SessionLocal

LOGGER = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_BATCH_SIZE = 5_000
_MAX_BATCHES_PER_TABLE = 10_000


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        return default


def _delete_older_than(
    *,
    table: str,
    timestamp_column: str,
    cutoff: datetime,
    batch_size: int,
) -> int:
    """
    Delete rows with ``timestamp_column < cutoff`` in bounded batches.

    Returns total rows deleted.
    """

    sql = text(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id
            FROM {table}
            WHERE {timestamp_column} < :cutoff
            ORDER BY {timestamp_column}
            LIMIT :batch_size
        )
        """
    )
    total = 0
    with SessionLocal() as session:
        for _ in range(_MAX_BATCHES_PER_TABLE):
            result = session.execute(sql, {"cutoff": cutoff, "batch_size": batch_size})
            deleted = int(result.rowcount or 0)
            session.commit()
            total += deleted
            if deleted == 0:
                break
            LOGGER.info(
                "retention deleted table=%s batch_rows=%s total_rows=%s cutoff=%s",
                table,
                deleted,
                total,
                cutoff.isoformat(),
            )
    return total


@celery_app.task(name="tasks.purge_old_scraper_data")
def purge_old_scraper_data() -> dict[str, Any]:
    """
    Purge ``scraped_listings``, ``car_zero_price_scrape_snapshot``, and
    ``iranjib_zero_price_scrape_snapshot`` rows older than ``DATA_RETENTION_DAYS``
    (default 30).
    """

    retention_days = _env_int("DATA_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS, minimum=1)
    batch_size = _env_int("DATA_RETENTION_BATCH_SIZE", _DEFAULT_BATCH_SIZE, minimum=100)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    LOGGER.info(
        "tasks.purge_old_scraper_data start retention_days=%s batch_size=%s cutoff=%s",
        retention_days,
        batch_size,
        cutoff.isoformat(),
    )

    listings_deleted = _delete_older_than(
        table="scraped_listings",
        timestamp_column="scraped_at",
        cutoff=cutoff,
        batch_size=batch_size,
    )
    snapshots_deleted = _delete_older_than(
        table="car_zero_price_scrape_snapshot",
        timestamp_column="created_at",
        cutoff=cutoff,
        batch_size=batch_size,
    )
    iranjib_snapshots_deleted = _delete_older_than(
        table="iranjib_zero_price_scrape_snapshot",
        timestamp_column="created_at",
        cutoff=cutoff,
        batch_size=batch_size,
    )

    payload: dict[str, Any] = {
        "success": True,
        "retention_days": retention_days,
        "cutoff_utc": cutoff.isoformat(),
        "scraped_listings_deleted": listings_deleted,
        "car_zero_price_scrape_snapshot_deleted": snapshots_deleted,
        "iranjib_zero_price_scrape_snapshot_deleted": iranjib_snapshots_deleted,
    }
    LOGGER.info("tasks.purge_old_scraper_data done %s", payload)
    return payload
