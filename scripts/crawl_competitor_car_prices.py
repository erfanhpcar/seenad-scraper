#!/usr/bin/env python3
"""
Full-catalog competitor zero / market prices (not limited to SeenAd watchlist).

Sources:
  - Khodro45 pricing: https://khodro45.com/pricing/
      API: https://khodro45.com/api/v1/pricing/dailycars/
  - Hamrah Mechanic carprice: https://www.hamrah-mechanic.com/carprice/
      SSR __NEXT_DATA__ on /carprice/{brandEnglishName}/

Usage:
  python scripts/crawl_competitor_car_prices.py --source all
  python scripts/crawl_competitor_car_prices.py --source khodro45 --limit 10 --dry-run
  docker compose exec -T backend python3 scripts/crawl_competitor_car_prices.py --source all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.hamrah_mechanic_carprice_service import run_hamrah_carprice_pipeline
from src.scraper.khodro45_pricing_service import run_khodro45_pricing_pipeline

DEFAULT_K45_OUT = Path("data/raw_khodro45_pricing_catalog.jsonl")
DEFAULT_HM_OUT = Path("data/raw_hamrahmechanic_carprice_catalog.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=("khodro45", "hamrah", "all"),
        default="all",
        help="Which competitor catalogue to fetch.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse only; do not write JSONL.")
    parser.add_argument("--limit", type=int, default=None, help="Max rows per source (smoke test).")
    parser.add_argument("--khodro45-output", type=Path, default=DEFAULT_K45_OUT)
    parser.add_argument("--hamrah-output", type=Path, default=DEFAULT_HM_OUT)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    summaries: dict[str, object] = {}
    exit_code = 0

    if args.source in ("khodro45", "all"):
        try:
            k45 = run_khodro45_pricing_pipeline(
                output_path=args.khodro45_output,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            summaries["khodro45"] = k45.as_dict()
            if k45.items_seen <= 0:
                exit_code = 1
        except Exception as exc:
            logging.exception("khodro45 pricing crawl failed")
            summaries["khodro45"] = {"error": str(exc)}
            exit_code = 1

    if args.source in ("hamrah", "all"):
        try:
            hm = run_hamrah_carprice_pipeline(
                output_path=args.hamrah_output,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            summaries["hamrah-mechanic"] = hm.as_dict()
            if hm.items_seen <= 0:
                exit_code = 1
        except Exception as exc:
            logging.exception("hamrah-mechanic carprice crawl failed")
            summaries["hamrah-mechanic"] = {"error": str(exc)}
            exit_code = 1

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
