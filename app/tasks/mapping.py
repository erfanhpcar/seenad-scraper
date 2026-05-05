from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from app.celery_app import celery_app
from scripts.run_daily_pipeline import ensure_pipeline_history_table, provider_coverage, store_pipeline_history
from scripts.relink_from_mapping import run_relink_pipeline

LOGGER = logging.getLogger(__name__)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _allow_car_reference_mutations_from_env() -> bool:
    """Default false for Celery so workers never seed car_reference unless explicitly enabled."""
    return os.getenv("MAPPING_ALLOW_CAR_REFERENCE_MUTATIONS", "0").strip().lower() in {"1", "true", "yes"}


@celery_app.task(name="tasks.mapping_task")
def mapping_task() -> dict[str, object]:
    """
    Full mapping pipeline (gaps, scores, relink). Celery default skips MANUAL_REFERENCE_SEEDS /
    ensure_car_reference unless MAPPING_ALLOW_CAR_REFERENCE_MUTATIONS=1.
    """
    return run_relink_pipeline(
        provider=os.getenv("MAPPING_PROVIDER", "divar"),
        fill_gap_top_n=_env_int("MAPPING_FILL_GAP_TOP_N", 20),
        min_score=_env_int("MAPPING_MIN_SCORE", 72),
        report_top_unlinked=_env_int("MAPPING_REPORT_TOP_UNLINKED", 15),
        needs_mapping_top=_env_int("MAPPING_NEEDS_TOP", 20),
        review_score_min=_env_int("MAPPING_REVIEW_SCORE_MIN", 58),
        review_score_max=_env_int("MAPPING_REVIEW_SCORE_MAX", 68),
        dry_run=os.getenv("MAPPING_DRY_RUN", "").strip().lower() in {"1", "true", "yes"},
        allow_car_reference_mutations=_allow_car_reference_mutations_from_env(),
    )


@celery_app.task(name="tasks.run_mapping_pipeline")
def run_mapping_pipeline_task() -> dict[str, object]:
    """
    Scheduled mapping pipeline task with history persistence.
    Always runs with dry_run=False so DB updates are applied.
    """
    provider = os.getenv("MAPPING_PROVIDER", "divar")
    started_at = datetime.now(timezone.utc)

    out = run_relink_pipeline(
        provider=provider,
        fill_gap_top_n=_env_int("MAPPING_FILL_GAP_TOP_N", 20),
        min_score=_env_int("MAPPING_MIN_SCORE", 72),
        report_top_unlinked=_env_int("MAPPING_REPORT_TOP_UNLINKED", 15),
        needs_mapping_top=_env_int("MAPPING_NEEDS_TOP", 20),
        review_score_min=_env_int("MAPPING_REVIEW_SCORE_MIN", 58),
        review_score_max=_env_int("MAPPING_REVIEW_SCORE_MAX", 68),
        dry_run=False,
        allow_car_reference_mutations=_allow_car_reference_mutations_from_env(),
    )

    ensure_pipeline_history_table()
    total_ads, linked_ads = provider_coverage(provider)
    finished_at = datetime.now(timezone.utc)
    store_pipeline_history(
        provider_name=provider,
        started_at=started_at,
        finished_at=finished_at,
        total_ads=total_ads,
        linked_ads=linked_ads,
        metadata={
            "trigger": "celery-beat",
            "task_name": "tasks.run_mapping_pipeline",
            "dry_run": False,
            "relink_step": "completed",
        },
    )
    LOGGER.info(
        "run_mapping_pipeline_task done provider=%s total=%s linked=%s",
        provider,
        total_ads,
        linked_ads,
    )
    return out
