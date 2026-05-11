#!/usr/bin/env python3
"""
One-shot ingestion: Bama ``/cad/api/price/hierarchy`` → RapidFuzz match on ``car_entities`` → ``car_zero_price`` upsert.

Run from repo root (or any cwd); adjusts ``sys.path`` like other scripts under ``scripts/``.

Example::

    python scripts/seed_zero_prices.py
    python scripts/seed_zero_prices.py --dry-run --page-size 50

Full catalogue crawl (recommended before fuzzy pre-mapping; live logs, no ``-T``)::

    docker compose exec backend python -u /app/scripts/seed_zero_prices.py --full-crawl

Apply DB migration ``013_car_zero_price_drop_bama_unique.sql`` if ``car_zero_price`` still has a unique constraint on ``bama_car_price_id``.
Apply ``012_car_zero_price_scrape_snapshot.sql`` and later migrations on ``seenad_db`` before writing snapshots.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.bama_price_service import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    DEFAULT_PAGE_SIZE_MAX,
    MIN_COMBINED_MATCH_SCORE,
    run_bama_zero_price_pipeline,
)

LOGGER = logging.getLogger("seed_zero_prices")


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and match but do not write to the database.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=(
            "Hierarchy API page size (fewer HTTP round-trips when higher; "
            f"default {DEFAULT_PAGE_SIZE}, clamped 10–{DEFAULT_PAGE_SIZE_MAX})."
        ),
    )
    capture_group = parser.add_mutually_exclusive_group()
    capture_group.add_argument(
        "--capture-raw",
        dest="capture_raw",
        action="store_true",
        default=None,
        help="Insert every hierarchy row into car_zero_price_scrape_snapshot (default).",
    )
    capture_group.add_argument(
        "--no-capture-raw",
        dest="capture_raw",
        action="store_false",
        help="Do not write raw snapshot rows; only refresh latest car_zero_price values.",
    )
    parser.add_argument(
        "--full-crawl",
        action="store_true",
        help=(
            "Shorthand: --capture-raw plus aggressive page_size for full-catalogue depth "
            f"(clamp to at least ~{DEFAULT_PAGE_SIZE_MAX})."
        ),
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="Explicit scrape batch UUID for raw snapshots (default: auto-generated when capturing).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Stop after N hierarchy pages (0 = unlimited full crawl). For smoke tests only.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (noisy).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _configure_logging(verbose=bool(args.verbose))
    page_size = max(10, min(int(args.page_size), DEFAULT_PAGE_SIZE_MAX))
    capture_raw = True if args.capture_raw is None else bool(args.capture_raw)
    if args.full_crawl:
        page_size = DEFAULT_PAGE_SIZE_MAX
        capture_raw = True
    max_pages = int(args.max_pages)
    max_hierarchy_pages = None if max_pages <= 0 else max_pages
    batch_id = (args.batch_id or "").strip() or None
    LOGGER.info(
        "seed_zero_prices start dry_run=%s page_size=%s capture_raw=%s max_pages=%s min_combined_score=%s",
        args.dry_run,
        page_size,
        capture_raw,
        max_hierarchy_pages,
        MIN_COMBINED_MATCH_SCORE,
    )
    summary = run_bama_zero_price_pipeline(
        page_size=page_size,
        dry_run=bool(args.dry_run),
        capture_raw_rows=capture_raw,
        batch_id=batch_id,
        max_hierarchy_pages=max_hierarchy_pages,
    )
    LOGGER.info(
        "seed_zero_prices counts | priced_rows=%s mapped_ok=%s upserted=%s "
        "rejected_score_lt_%s=%s no_entity_match=%s total_hierarchy_rows=%s",
        summary.items_with_price,
        summary.matched,
        summary.upserted,
        MIN_COMBINED_MATCH_SCORE,
        summary.rejected_low_score,
        summary.skipped_no_entity,
        summary.items_seen,
    )
    LOGGER.info(
        "BAMA_ZERO_PRICE_SCRAPE_SUCCESS success=true batch_id=%s raw_rows_persisted=%s upserted=%s pages=%s",
        summary.batch_id,
        summary.raw_rows_persisted,
        summary.upserted,
        summary.pages_fetched,
    )
    LOGGER.info(
        "seed_zero_prices finished batch_id=%s raw_rows=%s %s",
        summary.batch_id,
        summary.raw_rows_persisted,
        summary.as_celery_dict(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
