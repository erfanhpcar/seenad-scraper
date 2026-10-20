#!/usr/bin/env python3
"""One-off backfill of free-market USD/IRR daily closes from TGJU history."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.usd_irr_rate_service import run_usd_irr_rate_history_backfill


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Calendar-day lookback window (default: 30). Trading days only are stored.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report without writing to the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.days < 1:
        print("days must be >= 1", file=sys.stderr)
        return 1
    summary = run_usd_irr_rate_history_backfill(days=args.days, dry_run=args.dry_run)
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
