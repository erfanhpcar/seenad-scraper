"""Central Celery application; broker URL from CELERY_BROKER_URL."""

from __future__ import annotations

import os
from datetime import timedelta

from celery import Celery

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

if _beat_schedule:
    celery_app.conf.beat_schedule = _beat_schedule
