"""Registry for zero-price snapshot batch completion (Bama + IranJib)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    CarZeroPriceScrapeSnapshot,
    IranJibZeroPriceScrapeSnapshot,
    ZeroPriceScrapeBatch,
)

LOGGER: Final = logging.getLogger(__name__)

STATUS_IN_PROGRESS: Final[str] = "in_progress"
STATUS_COMPLETED: Final[str] = "completed"
STATUS_FAILED: Final[str] = "failed"


class ZeroPriceBatchInvalidCompletionError(ValueError):
    """Raised when a batch cannot be marked completed (empty or not in progress)."""


class ZeroPriceBatchSource(str, Enum):
    BAMA = "bama"
    IRANJIB = "iranjib"


def begin_zero_price_batch(db: Session, *, source: ZeroPriceBatchSource, batch_id: str) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(ZeroPriceScrapeBatch)
        .values(
            batch_id=batch_id,
            source=source.value,
            status=STATUS_IN_PROGRESS,
            started_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[ZeroPriceScrapeBatch.batch_id])
    )
    db.execute(stmt)


def count_snapshot_rows_for_batch(
    db: Session,
    *,
    source: ZeroPriceBatchSource,
    batch_id: str,
) -> int:
    snapshot_model = (
        CarZeroPriceScrapeSnapshot
        if source == ZeroPriceBatchSource.BAMA
        else IranJibZeroPriceScrapeSnapshot
    )
    total = db.execute(
        select(func.count())
        .select_from(snapshot_model)
        .where(snapshot_model.batch_id == batch_id)
    ).scalar_one()
    return int(total)


def complete_zero_price_batch(
    db: Session,
    *,
    source: ZeroPriceBatchSource,
    batch_id: str,
    raw_row_count: int,
) -> None:
    if raw_row_count <= 0:
        raise ZeroPriceBatchInvalidCompletionError(
            f"zero_price batch complete refused: raw_row_count={raw_row_count} batch_id={batch_id}"
        )

    batch_row = db.get(ZeroPriceScrapeBatch, batch_id)
    if batch_row is None or batch_row.source != source.value:
        raise ZeroPriceBatchInvalidCompletionError(
            f"zero_price batch complete refused: missing registry row batch_id={batch_id}"
        )
    if batch_row.status != STATUS_IN_PROGRESS:
        raise ZeroPriceBatchInvalidCompletionError(
            f"zero_price batch complete refused: status={batch_row.status} batch_id={batch_id}"
        )

    persisted_rows = count_snapshot_rows_for_batch(db, source=source, batch_id=batch_id)
    if persisted_rows <= 0:
        raise ZeroPriceBatchInvalidCompletionError(
            f"zero_price batch complete refused: no snapshot rows batch_id={batch_id}"
        )
    if persisted_rows != raw_row_count:
        LOGGER.warning(
            "zero_price batch row_count mismatch source=%s batch_id=%s counter=%s db=%s",
            source.value,
            batch_id,
            raw_row_count,
            persisted_rows,
        )
        raw_row_count = persisted_rows

    now = datetime.now(timezone.utc)
    db.execute(
        update(ZeroPriceScrapeBatch)
        .where(
            ZeroPriceScrapeBatch.batch_id == batch_id,
            ZeroPriceScrapeBatch.source == source.value,
            ZeroPriceScrapeBatch.status == STATUS_IN_PROGRESS,
        )
        .values(
            status=STATUS_COMPLETED,
            raw_row_count=raw_row_count,
            completed_at=now,
            updated_at=now,
        )
    )


def abort_zero_price_batch(db: Session, *, source: ZeroPriceBatchSource, batch_id: str) -> None:
    """Drop partial snapshot rows and mark the batch failed (safe for main to ignore)."""

    snapshot_model = (
        CarZeroPriceScrapeSnapshot
        if source == ZeroPriceBatchSource.BAMA
        else IranJibZeroPriceScrapeSnapshot
    )
    deleted = db.execute(delete(snapshot_model).where(snapshot_model.batch_id == batch_id))
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(ZeroPriceScrapeBatch)
        .values(
            batch_id=batch_id,
            source=source.value,
            status=STATUS_FAILED,
            completed_at=now,
            started_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[ZeroPriceScrapeBatch.batch_id],
            set_={
                "status": STATUS_FAILED,
                "completed_at": now,
                "updated_at": now,
            },
        )
    )
    db.execute(stmt)
    LOGGER.warning(
        "zero_price batch aborted source=%s batch_id=%s snapshot_rows_deleted=%s",
        source.value,
        batch_id,
        deleted.rowcount,
    )
