#!/usr/bin/env python3
"""
Live manual test for the snapshot-only Bama zero-price pipeline.

Fetches the real Bama hierarchy API and persists rows to ``car_zero_price_scrape_snapshot``.

Usage (from repo root, inside backend container recommended)::

    docker compose exec -T backend python -u /app/scripts/trigger_live_zero_price_test.py

Optional env (same as Celery task)::

    BAMA_ZERO_PRICE_PAGE_SIZE=500
    BAMA_ZERO_PRICE_MAX_PAGES=2          # smoke test only
    BAMA_ZERO_PRICE_DRY_RUN=1            # fetch only, no DB writes
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models import CarZeroPriceScrapeSnapshot  # noqa: E402
from src.scraper.bama_price_service import (  # noqa: E402
    DEFAULT_PAGE_SIZE_MAX,
    run_bama_zero_price_pipeline,
)

LOGGER = logging.getLogger(__name__)


def _production_page_size() -> int:
    raw = os.getenv("BAMA_ZERO_PRICE_PAGE_SIZE", "").strip()
    if raw.isdigit():
        return max(10, min(int(raw), DEFAULT_PAGE_SIZE_MAX))
    return DEFAULT_PAGE_SIZE_MAX


def _max_hierarchy_pages() -> int | None:
    raw = os.getenv("BAMA_ZERO_PRICE_MAX_PAGES", "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def _dry_run_enabled() -> bool:
    return os.getenv("BAMA_ZERO_PRICE_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


def _verify_db_session() -> None:
    with SessionLocal() as db:
        db.execute(text("SELECT 1")).scalar_one()
    LOGGER.info("database session OK")


def _count_snapshot_rows_for_batch(batch_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(CarZeroPriceScrapeSnapshot)
                .where(CarZeroPriceScrapeSnapshot.batch_id == batch_id)
            )
            or 0
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    page_size = _production_page_size()
    dry_run = _dry_run_enabled()
    max_pages = _max_hierarchy_pages()

    print("=== Bama zero-price live snapshot test ===")
    print(f"page_size={page_size} dry_run={dry_run} max_hierarchy_pages={max_pages or 'full crawl'}")

    try:
        _verify_db_session()
    except Exception as exc:
        print(f"ERROR: database not reachable: {exc}", file=sys.stderr)
        return 1

    try:
        summary = run_bama_zero_price_pipeline(
            page_size=page_size,
            dry_run=dry_run,
            capture_raw_rows=True,
            max_hierarchy_pages=max_pages,
        )
    except Exception as exc:
        LOGGER.exception("pipeline failed")
        print(f"ERROR: pipeline failed: {exc}", file=sys.stderr)
        return 1

    batch_id = summary.batch_id or "(none)"
    db_row_count = 0
    if summary.batch_id and not dry_run:
        db_row_count = _count_snapshot_rows_for_batch(summary.batch_id)

    print("\n=== Run summary ===")
    print(f"batch_id:                 {batch_id}")
    print(f"hierarchy rows seen:      {summary.items_seen}")
    print(f"API pages fetched:        {summary.pages_fetched}")
    print(f"raw rows inserted (run):  {summary.raw_rows_persisted}")
    if not dry_run and summary.batch_id:
        print(f"rows in DB for batch:     {db_row_count}")
    if dry_run:
        print("dry_run=true — no rows were written to car_zero_price_scrape_snapshot.")
    elif summary.raw_rows_persisted != db_row_count:
        print(
            "note: inserted count may differ from DB total when duplicate "
            "bama_car_price_id rows were skipped (ON CONFLICT DO NOTHING)."
        )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
