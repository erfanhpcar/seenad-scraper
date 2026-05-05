from __future__ import annotations

import logging
import os

from app.celery_app import celery_app
from app.listing_normalization import run_color_gearbox_normalization
from app.listings_clean_sync import sync_listings_clean_from_scraped
from scripts.relink_from_mapping import apply_provider_mapping_to_listings_only

LOGGER = logging.getLogger(__name__)


def _mapping_provider_name() -> str:
    return os.getenv("MAPPING_PROVIDER", "divar").strip() or "divar"


@celery_app.task(name="tasks.normalization_task")
def normalization_task() -> dict[str, int]:
    """Resolve raw_data color / gearbox strings to colors.id and gearbox_types.id."""
    color_n, gear_n = run_color_gearbox_normalization()
    return {"color_rows_updated": color_n, "gearbox_rows_updated": gear_n}


@celery_app.task(name="tasks.divar_mapping_relink_task")
def divar_mapping_relink_task() -> dict[str, object]:
    """
    Apply mapping_provider_data → scraped_listings.car_reference_id only.
    Never inserts into car_reference (see apply_provider_mapping_to_listings_only).
    """
    dry_run = os.getenv("DIVAR_MAPPING_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    out = apply_provider_mapping_to_listings_only(provider=_mapping_provider_name(), dry_run=dry_run)
    LOGGER.info("divar_mapping_relink_task done %s", out)
    return out


@celery_app.task(name="tasks.sync_listings_clean_task")
def sync_listings_clean_task() -> dict[str, int]:
    """Rebuild listings_clean snapshot rows from scraped_listings."""
    n = sync_listings_clean_from_scraped()
    LOGGER.info("sync_listings_clean_task upserted_rows=%s", n)
    return {"listings_clean_rows_upserted": n}
