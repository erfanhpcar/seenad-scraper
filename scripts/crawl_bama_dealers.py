#!/usr/bin/env python3
"""
API-first crawler for Bama.ir car dealer / showroom directory.

Phase 1: Paginate GET /prf/api/Corporation/search (CorporationType=car).
Phase 2: Fetch GET /prf/api/Corporation/{id} per dealer; append JSONL rows.

Usage:
  python scripts/crawl_bama_dealers.py
  python scripts/crawl_bama_dealers.py --limit-pages 2 --output data/raw_bama_dealers.jsonl -v
  python scripts/crawl_bama_dealers.py --resume --output data/raw_bama_dealers.jsonl

Dependencies (project requirements.txt):
  requests
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from requests import Response
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("bama-dealers-crawler")

LISTING_SITE_URL = "https://bama.ir/dealer/car"
SEARCH_API_URL = "https://bama.ir/prf/api/Corporation/search"
DETAIL_API_URL = "https://bama.ir/prf/api/Corporation"
DEFAULT_OUTPUT = Path("data/raw_bama_dealers.jsonl")
DEFAULT_PAGE_SIZE = 24
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DETAIL_DELAY_MIN = 1.2
DETAIL_DELAY_MAX = 3.0


@dataclass(frozen=True)
class ListingDealer:
    dealer_id: int
    title: str | None
    slug: str | None
    city: str | None
    address: str | None
    profile_url: str
    badge: str | None
    badge_value: str | None
    active_ads_count: int | None
    status: str | None
    logo_url: str | None
    banner_url: str | None
    member_since_persian: str | None
    member_since_gregorian: str | None
    listing_page_index: int
    listing_raw_row: dict[str, Any] = field(repr=False)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_phones(contact: dict[str, Any]) -> list[str]:
    phones: list[str] = []
    landlines = contact.get("landlineNumbers")
    if isinstance(landlines, list):
        for entry in landlines:
            if isinstance(entry, str) and entry.strip():
                phones.append(entry.strip())

    mobile = contact.get("mobile")
    if isinstance(mobile, str) and mobile.strip():
        phones.append(mobile.strip())
    elif isinstance(mobile, list):
        for entry in mobile:
            if isinstance(entry, str) and entry.strip():
                phones.append(entry.strip())

    mobile_numbers = contact.get("mobileNumbers")
    if isinstance(mobile_numbers, list):
        for entry in mobile_numbers:
            if isinstance(entry, str) and entry.strip():
                phones.append(entry.strip())

    return phones


def _parse_listing_row(row: dict[str, Any], *, page_index: int) -> ListingDealer | None:
    dealer_id = _optional_int(row.get("id"))
    if dealer_id is None:
        return None

    registered = _nested_dict(row.get("registeredAt"))
    tag = _nested_dict(row.get("tag")) if row.get("tag") is not None else {}
    status_obj = _nested_dict(row.get("status"))
    alias = row.get("alias")
    slug = alias.strip() if isinstance(alias, str) and alias.strip() else None

    return ListingDealer(
        dealer_id=dealer_id,
        title=row.get("title") if isinstance(row.get("title"), str) else None,
        slug=slug,
        city=None,
        address=row.get("address") if isinstance(row.get("address"), str) else None,
        profile_url=urljoin(LISTING_SITE_URL, f"/dealer/{dealer_id}"),
        badge=tag.get("displayName") if tag else None,
        badge_value=tag.get("value") if tag else None,
        active_ads_count=_optional_int(row.get("adsCount")),
        status=status_obj.get("value") if status_obj else None,
        logo_url=row.get("logo") if isinstance(row.get("logo"), str) else None,
        banner_url=row.get("banner") if isinstance(row.get("banner"), str) else None,
        member_since_persian=registered.get("persian") if registered else None,
        member_since_gregorian=registered.get("gregorian") if registered else None,
        listing_page_index=page_index,
        listing_raw_row=row,
    )


def _build_dealer_record(
    listing: ListingDealer,
    detail_payload: dict[str, Any],
) -> dict[str, Any]:
    data = _nested_dict(detail_payload.get("data"))
    contact = _nested_dict(data.get("contact"))
    ads_stats = _nested_dict(data.get("adsStatistics"))
    car_stats = _nested_dict(ads_stats.get("car"))
    tag = _nested_dict(data.get("tag")) if data.get("tag") is not None else {}
    coordinates = data.get("coordinates")
    status_obj = _nested_dict(data.get("status"))
    registered = _nested_dict(data.get("registeredAt"))

    title = data.get("title") if isinstance(data.get("title"), str) else listing.title
    address = data.get("address") if isinstance(data.get("address"), str) else listing.address
    alias = data.get("alias")
    slug = listing.slug
    if isinstance(alias, str) and alias.strip():
        slug = alias.strip()

    stock_count = _optional_int(car_stats.get("count"))
    if stock_count is None:
        stock_count = listing.active_ads_count

    scraped_at = datetime.now(timezone.utc).isoformat()

    return {
        "dealer_id": listing.dealer_id,
        "source": "bama.ir",
        "scraped_at": scraped_at,
        "title": title,
        "slug": slug,
        "city": listing.city,
        "profile_url": listing.profile_url,
        "phones": _extract_phones(contact),
        "address": address,
        "stock_count": stock_count,
        "badge": tag.get("displayName") if tag else listing.badge,
        "badge_value": tag.get("value") if tag else listing.badge_value,
        "status": status_obj.get("value") if status_obj else listing.status,
        "logo_url": data.get("logo") if isinstance(data.get("logo"), str) else listing.logo_url,
        "banner_url": data.get("banner") if isinstance(data.get("banner"), str) else listing.banner_url,
        "coordinates": coordinates,
        "activity_duration": data.get("activityDuration"),
        "social_networks": data.get("socialNetworks"),
        "registered_at": registered or {
            "persian": listing.member_since_persian,
            "gregorian": listing.member_since_gregorian,
        },
        "car_brands_count": len(car_stats.get("brands") or [])
        if isinstance(car_stats.get("brands"), list)
        else None,
        "listing_summary": {
            "page_index": listing.listing_page_index,
            "active_ads_count_listing": listing.active_ads_count,
            "member_since_persian": listing.member_since_persian,
            "member_since_gregorian": listing.member_since_gregorian,
        },
        "raw_payload": {
            "listing_search_row": listing.listing_raw_row,
            "detail": detail_payload,
        },
    }


class BamaDealersCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        limit_pages: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        delay_min: float = DETAIL_DELAY_MIN,
        delay_max: float = DETAIL_DELAY_MAX,
        max_retries: int = 3,
        timeout: float = 30.0,
        resume: bool = False,
    ) -> None:
        self.output_path = output_path
        self.limit_pages = limit_pages
        self.page_size = max(1, page_size)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.resume = resume

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        self._scraped_ids: set[int] = set()
        self._detail_success_count = 0

    def _sleep_detail_politeness(self) -> None:
        if self.delay_max <= 0:
            return
        low = min(self.delay_min, self.delay_max)
        high = max(self.delay_min, self.delay_max)
        time.sleep(random.uniform(low, high))

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response: Response = self._session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code >= 500:
                    raise RequestException(
                        f"HTTP {response.status_code} server error for {response.url}"
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"Expected JSON object from {response.url}")
                return payload
            except (RequestException, Timeout, ValueError) as exc:
                last_error = exc
                LOGGER.warning(
                    "Request failed (%s/%s) url=%s params=%s error=%s",
                    attempt,
                    self.max_retries,
                    url,
                    params,
                    exc,
                )
                if attempt < self.max_retries:
                    time.sleep(min(2.0 * attempt, 6.0))
        assert last_error is not None
        raise last_error

    def _load_existing_ids(self) -> None:
        if not self.resume or not self.output_path.is_file():
            return
        with self.output_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Skipping corrupt JSONL line %s", line_no)
                    continue
                dealer_id = _optional_int(row.get("dealer_id"))
                if dealer_id is not None:
                    self._scraped_ids.add(dealer_id)
        if self._scraped_ids:
            LOGGER.info(
                "Resume: skipping %s dealer(s) already present in %s",
                len(self._scraped_ids),
                self.output_path,
            )

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def collect_listings(self) -> list[ListingDealer]:
        seen: dict[int, ListingDealer] = {}
        page_index = 0
        pages_fetched = 0

        while True:
            if self.limit_pages is not None and pages_fetched >= self.limit_pages:
                LOGGER.info("Stopping listing phase: --limit-pages=%s", self.limit_pages)
                break

            params = {
                "CorporationType": "car",
                "pageIndex": page_index,
                "pageSize": self.page_size,
            }
            LOGGER.info("Listing API pageIndex=%s pageSize=%s", page_index, self.page_size)

            try:
                payload = self._request_json(SEARCH_API_URL, params=params)
            except Exception as exc:
                LOGGER.error(
                    "Listing pageIndex=%s failed after %s retries: %s",
                    page_index,
                    self.max_retries,
                    exc,
                )
                break

            rows = payload.get("data")
            if not isinstance(rows, list):
                LOGGER.error("Listing response missing data array at pageIndex=%s", page_index)
                break

            if not rows:
                LOGGER.info("Empty data array at pageIndex=%s; listing phase complete", page_index)
                break

            for row in rows:
                if not isinstance(row, dict):
                    continue
                listing = _parse_listing_row(row, page_index=page_index)
                if listing is not None:
                    seen.setdefault(listing.dealer_id, listing)

            metadata = _nested_dict(payload.get("metadata"))
            has_next = metadata.get("hasNext")
            LOGGER.info(
                "pageIndex=%s rows=%s unique_total=%s hasNext=%s totalCount=%s",
                page_index,
                len(rows),
                len(seen),
                has_next,
                metadata.get("totalCount"),
            )

            pages_fetched += 1
            page_index += 1

            if has_next is False:
                LOGGER.info("metadata.hasNext=false; listing phase complete")
                break

        return list(seen.values())

    def scrape_details(self, listings: list[ListingDealer]) -> tuple[int, int, int]:
        success = 0
        skipped_resume = 0
        failed = 0
        detail_attempts = 0

        for index, listing in enumerate(listings, start=1):
            if listing.dealer_id in self._scraped_ids:
                skipped_resume += 1
                continue

            if detail_attempts > 0:
                self._sleep_detail_politeness()
            detail_attempts += 1

            detail_url = f"{DETAIL_API_URL}/{listing.dealer_id}"
            LOGGER.info(
                "Detail %s/%s dealer_id=%s url=%s",
                index,
                len(listings),
                listing.dealer_id,
                detail_url,
            )

            try:
                detail_payload = self._request_json(detail_url)
            except Exception as exc:
                LOGGER.warning(
                    "Detail fetch failed dealer_id=%s error=%s — skipping",
                    listing.dealer_id,
                    exc,
                )
                failed += 1
                continue

            data = detail_payload.get("data")
            if not isinstance(data, dict):
                LOGGER.warning(
                    "Detail response missing data object dealer_id=%s — skipping",
                    listing.dealer_id,
                )
                failed += 1
                continue

            try:
                record = _build_dealer_record(listing, detail_payload)
                self._append_jsonl(record)
                self._scraped_ids.add(listing.dealer_id)
                self._detail_success_count += 1
                success += 1
            except Exception as exc:
                LOGGER.warning(
                    "Detail parse/serialize failed dealer_id=%s error=%s — skipping",
                    listing.dealer_id,
                    exc,
                )
                failed += 1

        return success, skipped_resume, failed

    def run(self) -> int:
        self._load_existing_ids()
        listings = self.collect_listings()
        LOGGER.info("Collected %s unique dealer listing(s)", len(listings))

        if not listings:
            LOGGER.warning("No listings collected; nothing to scrape")
            return 1

        success, skipped_resume, failed = self.scrape_details(listings)
        LOGGER.info(
            "Done. detail_success=%s detail_skipped_resume=%s detail_failed=%s output=%s",
            success,
            skipped_resume,
            failed,
            self.output_path.resolve(),
        )
        if success > 0 or skipped_resume > 0:
            return 0
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl Bama.ir car dealers via PRF JSON APIs into JSONL.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit-pages",
        type=int,
        default=None,
        metavar="N",
        help="Stop listing phase after N API pages (testing)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Listing API pageSize (default: {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip dealer_ids already present in the output JSONL",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="HTTP retries per request on transient failures",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose)

    crawler = BamaDealersCrawler(
        output_path=args.output,
        limit_pages=args.limit_pages,
        page_size=args.page_size,
        max_retries=args.max_retries,
        timeout=args.timeout,
        resume=args.resume,
    )
    return crawler.run()


if __name__ == "__main__":
    sys.exit(main())
