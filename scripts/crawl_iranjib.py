#!/usr/bin/env python3
"""
Crawl IranJib domestic + imported zero-km market prices into
``iranjib_zero_price_scrape_snapshot``.

Usage:
  python scripts/crawl_iranjib.py --dry-run -v
  python scripts/crawl_iranjib.py --kind domestic --limit 20
  docker compose exec -T backend python3 scripts/crawl_iranjib.py --dry-run
  docker compose exec -T backend python3 scripts/crawl_iranjib.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.iranjib_price_service import run_iranjib_zero_price_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write Postgres.")
    parser.add_argument(
        "--kind",
        choices=("domestic", "imported", "all"),
        default="all",
        help="Which catalogue page(s) to fetch (default both).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max data rows per page (smoke).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    page_kinds = None if args.kind == "all" else (args.kind,)
    summary = run_iranjib_zero_price_pipeline(
        dry_run=args.dry_run,
        page_kinds=page_kinds,
        limit=args.limit,
    )
    print(json.dumps(summary.as_celery_dict(), ensure_ascii=False, indent=2))
    if summary.items_seen <= 0:
        print("iranjib crawl produced zero rows", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
