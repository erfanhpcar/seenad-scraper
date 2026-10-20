"""Celery entrypoints for IranJib zero-kilometre price scraping."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from celery.exceptions import Ignore

from app.celery_app import celery_app
from src.scraper.iranjib_price_service import IranJibTransientError

LOGGER = logging.getLogger(__name__)

_RETRYABLE = (
    requests.RequestException,
    IranJibTransientError,
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
    name="tasks.iranjib_zero_price_scrape",
)
def iranjib_zero_price_scrape(self) -> dict[str, Any]:
    """
    Fetch domestic + imported IranJib catalogue pages; persist raw rows to
    ``iranjib_zero_price_scrape_snapshot`` only.

    Per-page HTTP retries live in ``fetch_price_page``. This Celery autoretry is the
    second line of defence when IranJib stays down for the whole page-attempt budget
    (60s / 120s / 240s). Empty/unparseable HTML is dumped under ``data/iranjib_fail_*``.
    Bounded by SoftTimeLimit / TimeLimit on the worker.
    """

    from src.scraper.iranjib_price_service import run_iranjib_zero_price_pipeline

    dry_run = os.getenv("IRANJIB_ZERO_PRICE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    retries = getattr(self.request, "retries", 0)
    task_id = getattr(self.request, "id", None)
    LOGGER.info(
        "tasks.iranjib_zero_price_scrape start dry_run=%s retry=%s/%s task_id=%s",
        dry_run,
        retries,
        self.max_retries,
        task_id,
    )
    try:
        summary = run_iranjib_zero_price_pipeline(dry_run=dry_run)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 403:
            LOGGER.critical(
                "CRITICAL: IP PROBABLY BANNED site=iranjib_zero_price status=403 task_id=%s",
                task_id,
            )
            raise Ignore() from exc
        LOGGER.warning(
            "iranjib_zero_price_scrape RETRY_TRIGGER kind=%s status=%s retry=%s/%s task_id=%s detail=%s",
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
            "iranjib_zero_price_scrape RETRY_TRIGGER kind=%s retry=%s/%s task_id=%s detail=%s",
            type(exc).__name__,
            retries,
            self.max_retries,
            task_id,
            exc,
        )
        raise

    payload = summary.as_celery_dict()
    LOGGER.info(
        "IRANJIB_ZERO_PRICE_SCRAPE_SUCCESS success=true batch_id=%s raw_rows_persisted=%s "
        "domestic=%s imported=%s iid=%s priced=%s retry=%s",
        summary.batch_id,
        summary.raw_rows_persisted,
        summary.domestic_rows,
        summary.imported_rows,
        summary.items_with_iid,
        summary.items_with_price,
        retries,
    )
    LOGGER.info("tasks.iranjib_zero_price_scrape done %s", payload)
    return payload
