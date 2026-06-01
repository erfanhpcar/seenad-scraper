#!/usr/bin/env python3
"""
Crawler: Modiran Khodro (مدیران خودرو) authorized dealership network.

Initializes a Laravel session on /dealersmvm, then POSTs to /find/dealers for each
provinceId (1–31) to collect dealer JSON records nationwide.

Usage:
  python scripts/crawl_modiran_khodro.py
  python scripts/crawl_modiran_khodro.py --output data/raw_modiran_khodro_dealers.json -v
  python scripts/crawl_modiran_khodro.py --min-province 1 --max-province 32

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("modiran-khodro-crawler")

BASE_URL = "https://www.mvmco.ir"
DEALERS_PAGE_URL = f"{BASE_URL}/dealersmvm"
FIND_DEALERS_URL = f"{BASE_URL}/find/dealers"
SOURCE_URL = DEALERS_PAGE_URL
DEFAULT_OUTPUT = Path("data/raw_modiran_khodro_dealers.json")
DEFAULT_MIN_PROVINCE = 1
DEFAULT_MAX_PROVINCE = 32
REQUEST_TIMEOUT_S = 15.0
DELAY_MIN_S = 1.5
DELAY_MAX_S = 3.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

GET_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


@dataclass
class DealershipRecord:
    """Flattened dealer row: all API keys plus crawl metadata."""

    province_name: str
    source_url: str
    scraped_at: str
    raw_payload: dict[str, Any]
    fields: dict[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        merged = dict(self.fields)
        merged["province_name"] = self.province_name
        merged["source_url"] = self.source_url
        merged["scraped_at"] = self.scraped_at
        merged["raw_payload"] = self.raw_payload
        return merged


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _extract_csrf_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta is None:
        return None
    content = meta.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _post_headers(csrf_token: str) -> dict[str, str]:
    return {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": DEALERS_PAGE_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _parse_dealer_item(
    dealer: dict[str, Any],
    *,
    province_name: str,
    scraped_at: str,
) -> DealershipRecord:
    return DealershipRecord(
        province_name=province_name,
        source_url=SOURCE_URL,
        scraped_at=scraped_at,
        raw_payload=copy.deepcopy(dealer),
        fields=copy.deepcopy(dealer),
    )


def _dealer_dedupe_key(record: DealershipRecord) -> str | int | None:
    dealer_id = record.fields.get("id")
    if isinstance(dealer_id, int):
        return dealer_id
    if isinstance(dealer_id, str) and dealer_id.strip():
        return dealer_id.strip()
    code = record.fields.get("code")
    name = record.fields.get("name")
    if code is not None and name is not None:
        return f"{code}:{name}"
    return None


class ModiranKhodroCrawler:
    def __init__(
        self,
        *,
        min_province: int = DEFAULT_MIN_PROVINCE,
        max_province: int = DEFAULT_MAX_PROVINCE,
        timeout: float = REQUEST_TIMEOUT_S,
        delay_min: float = DELAY_MIN_S,
        delay_max: float = DELAY_MAX_S,
    ) -> None:
        self.min_province = min_province
        self.max_province = max_province
        self.timeout = timeout
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.session = requests.Session()
        self.csrf_token: str | None = None

    def initialize_session(self) -> None:
        LOGGER.debug("GET %s", DEALERS_PAGE_URL)
        response = self.session.get(
            DEALERS_PAGE_URL,
            headers=GET_HEADERS,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Session init failed: GET returned HTTP {response.status_code}"
            )

        token = _extract_csrf_token(response.text)
        if not token:
            raise RuntimeError("CSRF token not found in dealers page HTML")

        self.csrf_token = token
        LOGGER.info(
            "Session initialized (%d cookies, CSRF token acquired)",
            len(self.session.cookies),
        )

    def fetch_province_dealers(self, province_id: int) -> tuple[str, list[dict[str, Any]]] | None:
        if not self.csrf_token:
            raise RuntimeError("Crawler session not initialized")

        response = self.session.post(
            FIND_DEALERS_URL,
            headers=_post_headers(self.csrf_token),
            data={"provinceId": str(province_id)},
            timeout=self.timeout,
        )

        if response.status_code != 200:
            LOGGER.warning(
                "Province %s: HTTP %s — skipping",
                province_id,
                response.status_code,
            )
            return None

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "Province %s: invalid JSON (%s) — skipping",
                province_id,
                exc,
            )
            return None

        if not isinstance(payload, dict):
            LOGGER.warning(
                "Province %s: expected object JSON, got %s — skipping",
                province_id,
                type(payload).__name__,
            )
            return None

        province_name = payload.get("provinceName")
        if not isinstance(province_name, str):
            province_name = ""

        dealers = payload.get("dealers")
        if dealers is None:
            dealers = []
        if not isinstance(dealers, list):
            LOGGER.warning(
                "Province %s: dealers is not a list — skipping",
                province_id,
            )
            return None

        dealer_dicts: list[dict[str, Any]] = []
        for entry in dealers:
            if isinstance(entry, dict):
                dealer_dicts.append(entry)

        return province_name, dealer_dicts

    def crawl(self) -> list[DealershipRecord]:
        self.initialize_session()
        all_records: list[DealershipRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()

        for province_id in range(self.min_province, self.max_province + 1):
            if province_id > self.min_province:
                delay = random.uniform(self.delay_min, self.delay_max)
                LOGGER.debug(
                    "Sleeping %.2fs before province %s",
                    delay,
                    province_id,
                )
                time.sleep(delay)

            try:
                result = self.fetch_province_dealers(province_id)
            except SSLError as exc:
                LOGGER.warning("Province %s: SSL error — %s", province_id, exc)
                continue
            except (Timeout, ReadTimeout) as exc:
                LOGGER.warning("Province %s: timeout — %s", province_id, exc)
                continue
            except ConnectionError as exc:
                LOGGER.warning("Province %s: connection error — %s", province_id, exc)
                continue
            except RequestException as exc:
                LOGGER.warning("Province %s: request failed — %s", province_id, exc)
                continue

            if result is None:
                print(
                    f"Scraping Province {province_id}: [request failed] - skipping"
                )
                continue

            province_name, dealers = result
            label = province_name or "(unknown)"
            count = len(dealers)

            print(
                f"Scraping Province {province_id}: [{label}] - "
                f"Found {count} dealers..."
            )

            if count == 0:
                continue

            for dealer in dealers:
                all_records.append(
                    _parse_dealer_item(
                        dealer,
                        province_name=province_name,
                        scraped_at=scraped_at,
                    )
                )

        return self._dedupe_records(all_records)

    @staticmethod
    def _dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
        seen: set[str | int] = set()
        unique: list[DealershipRecord] = []
        for record in records:
            key = _dealer_dedupe_key(record)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            unique.append(record)
        removed = len(records) - len(unique)
        if removed:
            LOGGER.info("Deduped %d duplicate dealer record(s)", removed)
        return unique


def write_json(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.to_dict() for record in records]
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Modiran Khodro (mvmco.ir) dealership network by province."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSON output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--min-province",
        type=int,
        default=DEFAULT_MIN_PROVINCE,
        help=f"First provinceId to request (default: {DEFAULT_MIN_PROVINCE})",
    )
    parser.add_argument(
        "--max-province",
        type=int,
        default=DEFAULT_MAX_PROVINCE,
        help=f"Last provinceId to request (default: {DEFAULT_MAX_PROVINCE})",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=DELAY_MIN_S,
        help=f"Minimum delay between provinces in seconds (default: {DELAY_MIN_S})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=DELAY_MAX_S,
        help=f"Maximum delay between provinces in seconds (default: {DELAY_MAX_S})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_S,
        help=f"HTTP timeout in seconds (default: {REQUEST_TIMEOUT_S})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.min_province < 1 or args.max_province < args.min_province:
        LOGGER.error("Invalid province range: %s–%s", args.min_province, args.max_province)
        return 1

    _configure_logging(args.verbose)

    crawler = ModiranKhodroCrawler(
        min_province=args.min_province,
        max_province=args.max_province,
        timeout=args.timeout,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )

    try:
        records = crawler.crawl()
    except RuntimeError as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    print(f"\nTotal: {len(records)} dealerships saved to {args.output.resolve()}")
    LOGGER.info("Wrote %d records to %s", len(records), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
