#!/usr/bin/env python3
"""
One-shot ingestion: Bama ``/cad/api/price/hierarchy`` → RapidFuzz match on ``car_entities`` → ``car_zero_price`` upsert.

Run from repo root (or any cwd); adjusts ``sys.path`` like other scripts under ``scripts/``.

Example::

    python scripts/seed_zero_prices.py
    python scripts/seed_zero_prices.py --dry-run --page-size 50
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.bama_price_service import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
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
        help=f"Hierarchy API page size (default {DEFAULT_PAGE_SIZE}, clamped 10–200).",
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
    page_size = max(10, min(int(args.page_size), 200))
    LOGGER.info(
        "seed_zero_prices start dry_run=%s page_size=%s min_combined_score=%s",
        args.dry_run,
        page_size,
        MIN_COMBINED_MATCH_SCORE,
    )
    summary = run_bama_zero_price_pipeline(page_size=page_size, dry_run=bool(args.dry_run))
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
    LOGGER.info("seed_zero_prices finished %s", summary.as_celery_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
