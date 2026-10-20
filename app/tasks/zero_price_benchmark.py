"""Celery: daily zero-price benchmark (SeenAd + K45 + Hamrah Mechanic)."""

from __future__ import annotations

import logging
from typing import Any

from celery.exceptions import Ignore

from app.celery_app import celery_app
from app.database import SessionLocal
from src.zero_price_benchmark.config import (
    LegacyBenchmarkWriterDisabled,
    legacy_benchmark_writer_allowed,
    legacy_benchmark_writer_block_reason,
)
from src.zero_price_benchmark.runner import run_daily_benchmark

LOGGER = logging.getLogger(__name__)


@celery_app.task(
    name="tasks.run_daily_zero_price_benchmark",
    bind=True,
    time_limit=14400,
    soft_time_limit=13800,
)
def run_daily_zero_price_benchmark(self) -> dict[str, Any]:
    if not legacy_benchmark_writer_allowed():
        reason = legacy_benchmark_writer_block_reason() or "legacy benchmark writer disabled"
        LOGGER.info("zero_price_benchmark skipped (%s)", reason)
        raise Ignore()

    task_id = getattr(self.request, "id", None)
    LOGGER.info("tasks.run_daily_zero_price_benchmark start task_id=%s", task_id)

    try:
        with SessionLocal() as session:
            summary = run_daily_benchmark(session)
    except LegacyBenchmarkWriterDisabled:
        LOGGER.info("zero_price_benchmark skipped (legacy writer guard)")
        raise Ignore() from None

    LOGGER.info("tasks.run_daily_zero_price_benchmark done %s", summary)
    return summary
