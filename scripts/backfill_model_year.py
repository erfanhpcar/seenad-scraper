from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
import re

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.listings_clean_sync import sync_listings_clean_from_scraped
from app.models import ScrapedListing
from app.tasks.ingestion import bama_ingestion_task, hm_ingestion_task, kn_ingestion_task

JALALI_YEAR_RE = re.compile(r"(?<!\d)(1[34]\d{2})(?!\d)")
TITLE_YEAR_RE = re.compile(r"(?<!\d)(1[34]\d{2}|19\d{2}|20\d{2})(?!\d)")
URL_END_YEAR_RE = re.compile(
    r"(?:[-_/]|%2[fF]|%2[dD])(1[34]\d{2}|19\d{2}|20\d{2})(?:[/#?]|$)"
)
FA_AR_TO_EN = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).translate(FA_AR_TO_EN)
    return " ".join(text.split())


def _normalize_to_jalali_year(year: int | None) -> int | None:
    if year is None:
        return None
    # Upstream feeds may expose Gregorian year; convert to Jalali for our model_year convention.
    if year > 1800:
        year = year - 621
    if 1300 <= year <= 1499:
        return year
    return None


def _extract_year_from_url_tail(url_text: str) -> int | None:
    match = URL_END_YEAR_RE.search(url_text)
    if not match:
        return None
    return _normalize_to_jalali_year(int(match.group(1)))


def _extract_year_from_title_text(title_text: str) -> int | None:
    # Prefer Jalali directly from title; fallback to broader year range then normalize.
    jalali_match = JALALI_YEAR_RE.search(title_text)
    if jalali_match:
        return _normalize_to_jalali_year(int(jalali_match.group(1)))
    title_match = TITLE_YEAR_RE.search(title_text)
    if title_match:
        return _normalize_to_jalali_year(int(title_match.group(1)))
    return None


def _extract_year_from_listing(row: ScrapedListing) -> int | None:
    raw = row.raw_data or {}
    url_text = _normalize_text(raw.get("url"))
    if url_text:
        url_year = _extract_year_from_url_tail(url_text)
        if url_year is not None:
            return url_year

    title_text = _normalize_text(raw.get("title"))
    if title_text:
        title_year = _extract_year_from_title_text(title_text)
        if title_year is not None:
            return title_year

    candidates = [
        raw.get("description"),
        raw.get("year"),
        row.trim_name,
        row.model_name,
        raw.get("trim"),
        raw.get("model"),
    ]
    for candidate in candidates:
        text = _normalize_text(candidate)
        if not text:
            continue
        match = TITLE_YEAR_RE.search(text)
        if not match:
            continue
        year = _normalize_to_jalali_year(int(match.group(1)))
        if year is not None:
            return year
    return None


def backfill_model_year(limit: int | None) -> tuple[int, int]:
    scanned = 0
    updated = 0
    with SessionLocal() as db:
        stmt = (
            select(ScrapedListing)
            .where(ScrapedListing.model_year.is_(None))
            .order_by(ScrapedListing.id.desc())
        )
        if limit and limit > 0:
            stmt = stmt.limit(limit)
        rows = db.scalars(stmt).all()
        for row in rows:
            scanned += 1
            year = _extract_year_from_listing(row)
            if year is None:
                continue
            row.model_year = year
            updated += 1
        db.commit()
    return scanned, updated


def enqueue_rescrape_for_missing(limit_per_source: int) -> dict[str, int]:
    source_to_task = {
        "bama": bama_ingestion_task,
        "karnameh": kn_ingestion_task,
        "hamrah-mechanic": hm_ingestion_task,
    }
    enqueued: dict[str, int] = defaultdict(int)
    with SessionLocal() as db:
        for source, task in source_to_task.items():
            stmt = (
                select(ScrapedListing)
                .where(ScrapedListing.source == source, ScrapedListing.model_year.is_(None))
                .order_by(ScrapedListing.scraped_at.desc())
                .limit(limit_per_source)
            )
            rows = db.scalars(stmt).all()
            for row in rows:
                raw = row.raw_data or {}
                url = _normalize_text(raw.get("url"))
                if not url.startswith("http"):
                    continue
                task.delay(url)
                enqueued[source] += 1
    return dict(enqueued)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill scraped_listings.model_year and optionally enqueue re-scrape.")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only scan latest N records with model_year IS NULL (0 means all).",
    )
    parser.add_argument(
        "--enqueue-missing-limit",
        type=int,
        default=0,
        help="If >0, enqueue latest N missing-year records per source for re-scrape.",
    )
    parser.add_argument(
        "--sync-clean",
        action="store_true",
        help="Run listings_clean sync after backfill/enqueue.",
    )
    parser.add_argument(
        "--force-all-nulls",
        action="store_true",
        help="Explicitly force scanning all rows where model_year IS NULL.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    limit = args.limit if args.limit > 0 else None
    if args.force_all_nulls:
        limit = None
    scanned, updated = backfill_model_year(limit=limit)
    print(f"Backfill scanned={scanned} updated={updated}")

    if args.enqueue_missing_limit > 0:
        enqueued = enqueue_rescrape_for_missing(args.enqueue_missing_limit)
        print(f"Re-scrape enqueued per source: {enqueued}")

    if args.sync_clean:
        upserted = sync_listings_clean_from_scraped()
        print(f"listings_clean sync rows_upserted={upserted}")


if __name__ == "__main__":
    main()
