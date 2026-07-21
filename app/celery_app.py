"""Central Celery application; broker URL from CELERY_BROKER_URL."""

from __future__ import annotations

import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

_broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
_backend = os.getenv("CELERY_RESULT_BACKEND", _broker)

celery_app = Celery(
    "seenad",
    broker=_broker,
    backend=_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
)

celery_app.autodiscover_tasks(["app.tasks"])


@worker_process_init.connect
def _reset_external_db_after_fork(**_kwargs: object) -> None:
    """Discard any SQL Server pool inherited from the Celery parent process."""
    from app.external_db import reset_external_db_engine

    reset_external_db_engine()

_beat_schedule: dict[str, dict[str, object]] = {}

# Bama-only Beat entries: ``tasks.bama_ingestion_tick`` (queue drain) and ``tasks.bama_discovery``.
# Set BAMA_INGESTION_BEAT_ENABLED / BAMA_DISCOVERY_BEAT_ENABLED to 0 (e.g. seenad_beat in docker-compose)
# to pause; Divar/normalization stays on ``normalizer_scheduler`` → ``tasks.ingestion_task``.
_beat_enabled = os.getenv("BAMA_INGESTION_BEAT_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
if _beat_enabled:
    try:
        _bama_interval_s = int(os.getenv("BAMA_INGESTION_INTERVAL_SECONDS", "30"))
    except ValueError:
        _bama_interval_s = 30
    _bama_interval_s = max(_bama_interval_s, 10)
    _beat_schedule["bama-ingestion-queue-every-interval"] = {
        "task": "tasks.bama_ingestion_tick",
        "schedule": timedelta(seconds=_bama_interval_s),
    }

_discovery_beat_enabled = os.getenv("BAMA_DISCOVERY_BEAT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
if _discovery_beat_enabled:
    try:
        _discovery_interval_s = int(os.getenv("BAMA_DISCOVERY_INTERVAL_SECONDS", "300"))
    except ValueError:
        _discovery_interval_s = 300
    _discovery_interval_s = max(_discovery_interval_s, 60)
    _beat_schedule["bama-discovery-every-interval"] = {
        "task": "tasks.bama_discovery",
        "schedule": timedelta(seconds=_discovery_interval_s),
    }

_listing_feeder_beat_enabled = os.getenv("LISTING_FEEDER_BEAT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
if _listing_feeder_beat_enabled:
    try:
        _listing_feeder_interval_s = int(os.getenv("LISTING_FEEDER_INTERVAL_SECONDS", "1800"))
    except ValueError:
        _listing_feeder_interval_s = 1800
    _listing_feeder_interval_s = max(_listing_feeder_interval_s, 300)
    _beat_schedule["listing-feeder-every-interval"] = {
        "task": "tasks.crawl_listing_pages",
        "schedule": timedelta(seconds=_listing_feeder_interval_s),
    }

_kn_beat_enabled = os.getenv("KN_INGESTION_BEAT_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
if _kn_beat_enabled:
    try:
        _kn_interval_s = int(os.getenv("KN_INGESTION_INTERVAL_SECONDS", "30"))
    except ValueError:
        _kn_interval_s = 30
    _kn_interval_s = max(_kn_interval_s, 10)
    _beat_schedule["kn-ingestion-queue-every-interval"] = {
        "task": "tasks.kn_ingestion_tick",
        "schedule": timedelta(seconds=_kn_interval_s),
    }

_hm_beat_enabled = os.getenv("HM_INGESTION_BEAT_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
if _hm_beat_enabled:
    try:
        _hm_interval_s = int(os.getenv("HM_INGESTION_INTERVAL_SECONDS", "30"))
    except ValueError:
        _hm_interval_s = 30
    _hm_interval_s = max(_hm_interval_s, 10)
    _beat_schedule["hm-ingestion-queue-every-interval"] = {
        "task": "tasks.hm_ingestion_tick",
        "schedule": timedelta(seconds=_hm_interval_s),
    }

_mapping_pipeline_beat_enabled = os.getenv("MAPPING_PIPELINE_BEAT_ENABLED", "0").strip().lower() not in {
    "0",
    "false",
    "no",
}
if _mapping_pipeline_beat_enabled:
    try:
        _mapping_pipeline_interval_s = int(os.getenv("MAPPING_PIPELINE_INTERVAL_SECONDS", "3600"))
    except ValueError:
        _mapping_pipeline_interval_s = 3600
    _mapping_pipeline_interval_s = max(_mapping_pipeline_interval_s, 60)
    _beat_schedule["mapping-pipeline-every-60-minutes"] = {
        "task": "tasks.run_mapping_pipeline",
        "schedule": timedelta(seconds=_mapping_pipeline_interval_s),
    }

_zero_price_beat_enabled = os.getenv("BAMA_ZERO_PRICE_BEAT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
if _zero_price_beat_enabled:
    # Twice daily Asia/Tehran (Iran Standard Time, no DST):
    #   10:00 → 06:30 UTC
    #   16:00 → 12:30 UTC
    # Override with BAMA_ZERO_PRICE_BEAT_HOURS_UTC (comma-separated) and
    # BAMA_ZERO_PRICE_BEAT_MINUTE_UTC. Legacy BAMA_ZERO_PRICE_BEAT_HOUR_UTC still works.
    try:
        _zero_price_minute = int(os.getenv("BAMA_ZERO_PRICE_BEAT_MINUTE_UTC", "30"))
    except ValueError:
        _zero_price_minute = 30

    _hours_raw = os.getenv("BAMA_ZERO_PRICE_BEAT_HOURS_UTC", "").strip()
    if not _hours_raw:
        _legacy_hour = os.getenv("BAMA_ZERO_PRICE_BEAT_HOUR_UTC", "").strip()
        _hours_raw = _legacy_hour if _legacy_hour else "6,12"
    try:
        _zero_price_hours = tuple(
            int(part.strip()) for part in _hours_raw.split(",") if part.strip() != ""
        )
        if not _zero_price_hours:
            raise ValueError("empty hours")
    except ValueError:
        _zero_price_hours = (6, 12)

    _beat_schedule["bama-zero-price-twice-daily"] = {
        "task": "tasks.bama_zero_price_scrape",
        "schedule": crontab(hour=_zero_price_hours, minute=_zero_price_minute),
    }

_retention_beat_enabled = os.getenv("DATA_RETENTION_BEAT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
if _retention_beat_enabled:
    # Nightly 02:00 Asia/Tehran (Iran Standard Time, no DST) → 22:30 UTC.
    # Override with DATA_RETENTION_BEAT_HOUR_UTC / DATA_RETENTION_BEAT_MINUTE_UTC.
    try:
        _retention_hour = int(os.getenv("DATA_RETENTION_BEAT_HOUR_UTC", "22"))
        _retention_minute = int(os.getenv("DATA_RETENTION_BEAT_MINUTE_UTC", "30"))
    except ValueError:
        _retention_hour, _retention_minute = 22, 30
    _beat_schedule["purge-old-scraper-data-nightly"] = {
        "task": "tasks.purge_old_scraper_data",
        "schedule": crontab(hour=_retention_hour, minute=_retention_minute),
    }

if _beat_schedule:
    celery_app.conf.beat_schedule = _beat_schedule
