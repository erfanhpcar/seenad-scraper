#!/usr/bin/env python3
"""
Fetch one Bama listing page and persist to scraped_listings (same path as bama_ingestion_task).
Usage:
  python scripts/debug_bama.py [URL]
  BAMA_DEBUG_URL=https://bama.ir/car/... python scripts/debug_bama.py
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

from sqlalchemy.exc import IntegrityError, SQLAlchemyError  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.scrapers.bama_detail_parser import fetch_and_parse_bama_listing  # noqa: E402
from app.tasks.ingestion import (  # noqa: E402
    _ensure_car_reference_row,
    _upsert_bama_scraped_listing,
    _validate_bama_extract_for_persist,
)


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BAMA_DEBUG_URL", "")).strip()
    if not url:
        print("Pass a URL argument or set BAMA_DEBUG_URL.", file=sys.stderr)
        return 2

    print("--- debug_bama: fetching + parsing ---", flush=True)
    try:
        extract = fetch_and_parse_bama_listing(url)
    except Exception as exc:
        print(f"FETCH/PARSE FAILED: {exc}", flush=True)
        traceback.print_exc()
        return 1

    price_irr_int = int(extract.price) if extract.price is not None else None
    print(
        f"extract: external_id={extract.external_id!r} "
        f"price_decimal={extract.price!r} price_irr_integer={price_irr_int!r} "
        f"(stored unit: IRR / ریال, same as schema.org priceCurrency) "
        f"negotiable={extract.price_is_negotiable!r} "
        f"price_debug_note={extract.price_debug_note!r}",
        flush=True,
    )
    print(
        f"extract: mileage={extract.mileage!r} city={extract.city!r} color={extract.color!r} "
        f"gearbox/transmission={extract.transmission!r} brand={extract.brand!r} model={extract.model!r} "
        f"trim={extract.trim!r} url={extract.listing_url!r}",
        flush=True,
    )

    try:
        _validate_bama_extract_for_persist(extract)
    except ValueError as exc:
        print(f"VALIDATION FAILED (no DB write): {exc}", flush=True)
        return 1

    print("--- debug_bama: DB session ---", flush=True)
    with SessionLocal() as db:
        try:
            ref = _ensure_car_reference_row(
                db, extract.brand, extract.model, extract.trim, extract.listing_url
            )
            print(f"car_reference id={ref.id}", flush=True)
            listing = _upsert_bama_scraped_listing(db, extract, ref.id)
            db.flush()
            print(f"after flush scraped_listings.id={getattr(listing, 'id', None)!r}", flush=True)
            db.commit()
            print("COMMIT OK", flush=True)
        except IntegrityError as exc:
            db.rollback()
            print(f"IntegrityError: {exc}", flush=True)
            if exc.orig is not None:
                print(f"  orig: {exc.orig}", flush=True)
            traceback.print_exc()
            return 1
        except SQLAlchemyError as exc:
            db.rollback()
            print(f"SQLAlchemyError: {exc}", flush=True)
            traceback.print_exc()
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
