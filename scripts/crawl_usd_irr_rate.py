#!/usr/bin/env python3
"""Fetch TGJU free-market USD and upsert usd_irr_rate for Tehran today."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.usd_irr_rate_service import run_usd_irr_rate_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    summary = run_usd_irr_rate_pipeline(dry_run=args.dry_run)
    print(json.dumps(summary.as_celery_dict(), ensure_ascii=False, indent=2))
    if not summary.success:
        print(
            f"usd_irr_rate today's row NOT created as_of_date={summary.as_of_date}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
