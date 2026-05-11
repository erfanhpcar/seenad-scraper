"""Central Celery application; broker URL from CELERY_BROKER_URL."""

from __future__ import annotations

import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

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

_mapping_pipeline_beat_enabled = os.getenv("MAPPING_PIPELINE_BEAT_ENABLED", "1").strip().lower() not in {
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
    # Daily 10:00 Asia/Tehran (Iran Standard Time, no DST): equivalent to 06:30 UTC.
    # Override with BAMA_ZERO_PRICE_BEAT_HOUR_UTC / BAMA_ZERO_PRICE_BEAT_MINUTE_UTC if needed.
    try:
        _zero_price_hour = int(os.getenv("BAMA_ZERO_PRICE_BEAT_HOUR_UTC", "6"))
        _zero_price_minute = int(os.getenv("BAMA_ZERO_PRICE_BEAT_MINUTE_UTC", "30"))
    except ValueError:
        _zero_price_hour, _zero_price_minute = 6, 30
    _beat_schedule["bama-zero-price-daily"] = {
        "task": "tasks.bama_zero_price_scrape",
        "schedule": crontab(hour=_zero_price_hour, minute=_zero_price_minute),
    }

if _beat_schedule:
    celery_app.conf.beat_schedule = _beat_schedule
