#!/usr/bin/env python3
"""
Re-fetch recent Bama listings and refresh body-condition contract fields in raw_data.

Usage:
  python scripts/backfill_bama_body_condition.py [--days 7] [--limit 500] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ScrapedListing
from app.scrapers.bama_detail_parser import fetch_and_parse_bama_listing
from app.tasks.ingestion import _build_bama_raw_payload, _upsert_bama_scraped_listing

LOGGER = logging.getLogger(__name__)


def _listing_url(record: ScrapedListing) -> str | None:
    raw = record.raw_data or {}
    url = raw.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def backfill_bama_body_condition(*, days: int, limit: int, dry_run: bool) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    updated = 0
    skipped = 0
    failed = 0

    with SessionLocal() as db:
        rows = db.scalars(
            select(ScrapedListing)
            .where(
                ScrapedListing.source == "bama",
                ScrapedListing.scraped_at >= cutoff,
            )
            .order_by(ScrapedListing.scraped_at.desc())
            .limit(limit)
        ).all()

        LOGGER.info("Found %s bama rows in last %s days (limit=%s)", len(rows), days, limit)

        for record in rows:
            url = _listing_url(record)
            if not url:
                skipped += 1
                continue
            try:
                extract = fetch_and_parse_bama_listing(url)
            except Exception:
                LOGGER.warning("Fetch/parse failed external_id=%r url=%s", record.external_id, url, exc_info=True)
                failed += 1
                continue

            if not extract.body_condition:
                skipped += 1
                continue

            incoming = _build_bama_raw_payload(extract)
            if not incoming.get("body_condition_summary"):
                skipped += 1
                continue

            if dry_run:
                LOGGER.info(
                    "DRY RUN external_id=%r summary=%r status=%r",
                    record.external_id,
                    incoming.get("body_condition_summary"),
                    incoming.get("body_status"),
                )
                updated += 1
                continue

            _upsert_bama_scraped_listing(db, extract)
            updated += 1

        if not dry_run:
            db.commit()

    LOGGER.info("Backfill done updated=%s skipped=%s failed=%s dry_run=%s", updated, skipped, failed, dry_run)
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Bama body condition contract fields")
    parser.add_argument("--days", type=int, default=7, help="Look back window in days (default: 7)")
    parser.add_argument("--limit", type=int, default=500, help="Max rows to process (default: 500)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write to DB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    return backfill_bama_body_condition(days=args.days, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
