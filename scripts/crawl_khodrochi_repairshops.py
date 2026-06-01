#!/usr/bin/env python3
"""
Two-step crawler for Khodrochi repair shop directory (listing + detail).

Phase 1: Paginate ?page=N until a listing page has no a.repairshop cards.
Phase 2: Fetch each item.php detail page; append one JSONL row per success.

Usage:
  python scripts/crawl_khodrochi_repairshops.py
  python scripts/crawl_khodrochi_repairshops.py --limit-pages 2 --limit-details 5 -v
  python scripts/crawl_khodrochi_repairshops.py --output data/raw_khodrochi_repairshops.jsonl

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests import Response
from requests.exceptions import RequestException

LOGGER = logging.getLogger("khodrochi-repairshop-crawler")

BASE_URL = "https://khodrochi.ir/repairshop/"
DEFAULT_OUTPUT = Path("data/raw_khodrochi_repairshops.jsonl")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
COORD_RE = re.compile(
    r"query=([-\d.]+),([-\d.]+)|@([-\d.]+),([-\d.]+)|!3d([-\d.]+)!4d([-\d.]+)",
    re.IGNORECASE,
)
WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ListingItem:
    item_id: str
    detail_url: str
    name: str | None
    specialty_tags: list[str] = field(default_factory=list)
    verified: bool = False
    excerpt: str | None = None
    address_listing: str | None = None
    image_url: str | None = None
    listing_page: int | None = None


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = WS_RE.sub(" ", value.replace("\xa0", " ")).strip()
    text = re.sub(r"[:：]\s*$", "", text).strip()
    return text or None


def _listing_page_url(page: int) -> str:
    if page <= 1:
        return BASE_URL
    return f"{BASE_URL}?page={page}"


class KhodrochiCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        limit_pages: int | None = None,
        limit_details: int | None = None,
        delay_min: float = 1.0,
        delay_max: float = 2.5,
        max_retries: int = 3,
        timeout: float = 30.0,
        resume: bool = False,
    ) -> None:
        self.output_path = output_path
        self.limit_pages = limit_pages
        self.limit_details = limit_details
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.resume = resume

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        self._scraped_ids: set[str] = set()
        self._detail_count = 0

    def _sleep_detail_politeness(self) -> None:
        if self.delay_max <= 0:
            return
        low = min(self.delay_min, self.delay_max)
        high = max(self.delay_min, self.delay_max)
        time.sleep(random.uniform(low, high))

    def _fetch_html(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response: Response = self._session.get(url, timeout=self.timeout)
                response.raise_for_status()
                response.encoding = response.apparent_encoding or "utf-8"
                return response.text
            except RequestException as exc:
                last_error = exc
                LOGGER.warning(
                    "Request failed (%s/%s) url=%s error=%s",
                    attempt,
                    self.max_retries,
                    url,
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
                item_id = row.get("item_id")
                if item_id:
                    self._scraped_ids.add(str(item_id))
        if self._scraped_ids:
            LOGGER.info("Resume: skipping %s already-scraped item(s)", len(self._scraped_ids))

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _extract_item_id(href: str, base_url: str) -> str | None:
        if not href:
            return None
        absolute = urljoin(base_url, href)
        qs = parse_qs(urlparse(absolute).query)
        raw = qs.get("id", [None])[0]
        if raw:
            return str(raw)
        match = re.search(r"id=(\d+)", href, re.IGNORECASE)
        return match.group(1) if match else None

    def parse_listing_page(self, html: str, *, page: int) -> list[ListingItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[ListingItem] = []

        for anchor in soup.select("a.repairshop"):
            href = anchor.get("href", "")
            item_id = self._extract_item_id(href, BASE_URL)
            if not item_id:
                continue

            title_el = anchor.select_one(".rs_title span")
            name = _clean_text(title_el.get_text(strip=True) if title_el else None)

            tags = [
                _clean_text(tag.get_text(strip=True)) or ""
                for tag in anchor.select(".rs_field")
            ]
            tags = [t for t in tags if t]

            excerpt_el = anchor.select_one(".rs_excerpt")
            excerpt = _clean_text(excerpt_el.get_text(strip=True) if excerpt_el else None)

            address_el = anchor.select_one(".rs_address")
            address_listing = _clean_text(
                address_el.get_text(" ", strip=True) if address_el else None
            )

            img_el = anchor.select_one("img.rs_img")
            image_url = None
            if img_el and img_el.get("src"):
                image_url = urljoin(BASE_URL, img_el["src"])

            detail_url = urljoin(BASE_URL, href)

            items.append(
                ListingItem(
                    item_id=item_id,
                    detail_url=detail_url,
                    name=name,
                    specialty_tags=tags,
                    verified=bool(anchor.select_one(".fa-crown")),
                    excerpt=excerpt,
                    address_listing=address_listing,
                    image_url=image_url,
                    listing_page=page,
                )
            )

        return items

    def collect_listings(self) -> list[ListingItem]:
        seen: dict[str, ListingItem] = {}
        page = 1

        while True:
            if self.limit_pages is not None and page > self.limit_pages:
                LOGGER.info("Stopping listing phase: --limit-pages=%s", self.limit_pages)
                break

            url = _listing_page_url(page)
            LOGGER.info("Listing page %s: %s", page, url)

            try:
                html = self._fetch_html(url)
            except RequestException as exc:
                LOGGER.error("Listing page %s failed after retries: %s", page, exc)
                break

            cards = self.parse_listing_page(html, page=page)
            if not cards:
                LOGGER.info("No repairshop cards on page %s; listing phase complete", page)
                break

            for card in cards:
                seen.setdefault(card.item_id, card)

            LOGGER.info("Page %s: %s card(s), %s unique total", page, len(cards), len(seen))
            page += 1

        return list(seen.values())

    @staticmethod
    def _parse_structured_info(soup: BeautifulSoup) -> dict[str, str]:
        info: dict[str, str] = {}
        for block in soup.select(".rsi_list_item.rsi_list_item2 div"):
            text = _clean_text(block.get_text(strip=True))
            if not text:
                continue
            if text.startswith("- "):
                text = text[2:]
            if ":" not in text:
                continue
            key, _, value = text.partition(":")
            key = _clean_text(key)
            value = _clean_text(value)
            if key and value:
                info[key] = value
        return info

    @staticmethod
    def _extract_coordinates(html: str, soup: BeautifulSoup) -> tuple[float | None, float | None]:
        candidates: list[str] = []
        for anchor in soup.select('a[href*="google.com/maps"], a[href*="maps.google"]'):
            href = anchor.get("href", "")
            if href:
                candidates.append(href)

        for iframe in soup.select("iframe[src*='google.com/maps'], iframe[src*='maps']"):
            src = iframe.get("src", "")
            if src:
                candidates.append(src)

        for match in COORD_RE.finditer(html):
            groups = [g for g in match.groups() if g is not None]
            if len(groups) >= 2:
                try:
                    return float(groups[0]), float(groups[1])
                except ValueError:
                    continue

        for candidate in candidates:
            match = COORD_RE.search(candidate)
            if not match:
                continue
            groups = [g for g in match.groups() if g is not None]
            if len(groups) >= 2:
                try:
                    return float(groups[0]), float(groups[1])
                except ValueError:
                    continue

        return None, None

    @staticmethod
    def _extract_phone(soup: BeautifulSoup, structured: dict[str, str]) -> str | None:
        for selector in (
            'a.spec_col_link.scl2[href^="tel:"]',
            'a[href^="tel:"].rsil',
            'a[href^="tel:"]',
        ):
            tel_anchor = soup.select_one(selector)
            if tel_anchor and tel_anchor.get("href"):
                return _clean_text(tel_anchor["href"].replace("tel:", "", 1))

        for key in ("شماره تماس", "تلفن", "موبایل"):
            if key in structured:
                return structured[key]

        phone_match = re.search(
            r"(?:تلفن|موبایل|شماره)[^\d]{0,20}(\+?\d[\d\s\-]{8,})",
            soup.get_text(" ", strip=True),
        )
        if phone_match:
            return _clean_text(phone_match.group(1))
        return None

    @staticmethod
    def _extract_gallery(soup: BeautifulSoup, base_url: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        for container_selector in (".rsi_list_img", ".rsi_list"):
            container = soup.select_one(container_selector)
            if not container:
                continue
            for anchor in container.select("a.rsi_list_item_img.gallery[href], a.gallery[href]"):
                href = anchor.get("href")
                if not href:
                    continue
                absolute = urljoin(base_url, href)
                if absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)

        return urls

    @staticmethod
    def _extract_services(soup: BeautifulSoup) -> dict[str, Any]:
        offered: list[str] = []
        not_offered: list[str] = []
        by_category: dict[str, list[str]] = {}

        for block in soup.select(".rsi__serv_list"):
            title_el = block.select_one(".rsi_title")
            category = _clean_text(title_el.get_text(strip=True) if title_el else None) or "general"
            category_offered: list[str] = []

            for row in block.select(".rsi_list_item_service"):
                label_el = row.find("div")
                if not label_el:
                    continue
                label = _clean_text(label_el.get_text(strip=True))
                if not label:
                    continue

                icon = row.select_one("i")
                classes = icon.get("class") if icon else []
                class_str = " ".join(classes) if classes else ""

                if "fa-check-circle" in class_str:
                    offered.append(label)
                    category_offered.append(label)
                elif "fa-times-circle" in class_str:
                    not_offered.append(label)

            if category_offered:
                by_category[category] = category_offered

        return {
            "offered": offered,
            "not_offered": not_offered,
            "by_category": by_category,
        }

    def parse_detail_page(self, html: str, listing: ListingItem) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        structured = self._parse_structured_info(soup)

        title_el = soup.select_one("h1")
        title = _clean_text(title_el.get_text(strip=True) if title_el else None) or listing.name

        phone = self._extract_phone(soup, structured)
        lat, lng = self._extract_coordinates(html, soup)

        address = structured.get("آدرس")
        introduction = structured.get("معرفی مجموعه")

        gallery_images = self._extract_gallery(soup, listing.detail_url)
        services = self._extract_services(soup)

        scraped_at = datetime.now(timezone.utc).isoformat()

        return {
            "item_id": listing.item_id,
            "detail_url": listing.detail_url,
            "source": "khodrochi.ir",
            "scraped_at": scraped_at,
            "title": title,
            "phone": phone,
            "latitude": lat,
            "longitude": lng,
            "address": address,
            "introduction": introduction,
            "gallery_images": gallery_images,
            "services": services,
            "specialty_tags": listing.specialty_tags,
            "verified": listing.verified,
            "excerpt": listing.excerpt,
            "address_listing": listing.address_listing,
            "image_url_listing": listing.image_url,
            "listing_page": listing.listing_page,
            "structured_info": structured,
            "raw_payload": {
                "html": html,
                "structured_info": structured,
                "listing": asdict(listing),
            },
        }

    def scrape_details(self, listings: list[ListingItem]) -> tuple[int, int]:
        success = 0
        skipped = 0

        for index, listing in enumerate(listings, start=1):
            if self.limit_details is not None and self._detail_count >= self.limit_details:
                LOGGER.info("Stopping detail phase: --limit-details=%s", self.limit_details)
                break

            if listing.item_id in self._scraped_ids:
                skipped += 1
                continue

            if index > 1 or self._detail_count > 0:
                self._sleep_detail_politeness()

            LOGGER.info(
                "Detail %s/%s id=%s url=%s",
                index,
                len(listings),
                listing.item_id,
                listing.detail_url,
            )

            try:
                html = self._fetch_html(listing.detail_url)
                record = self.parse_detail_page(html, listing)
                self._append_jsonl(record)
                self._scraped_ids.add(listing.item_id)
                self._detail_count += 1
                success += 1
            except RequestException as exc:
                LOGGER.warning(
                    "Detail fetch failed id=%s url=%s error=%s — skipping",
                    listing.item_id,
                    listing.detail_url,
                    exc,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Detail parse failed id=%s url=%s error=%s — skipping",
                    listing.item_id,
                    listing.detail_url,
                    exc,
                )

        return success, skipped

    def run(self) -> int:
        self._load_existing_ids()
        listings = self.collect_listings()
        LOGGER.info("Collected %s unique listing item(s)", len(listings))

        if not listings:
            LOGGER.warning("No listings collected; nothing to scrape")
            return 1

        success, skipped = self.scrape_details(listings)
        LOGGER.info(
            "Done. detail_success=%s detail_skipped_resume=%s output=%s",
            success,
            skipped,
            self.output_path.resolve(),
        )
        return 0 if success > 0 or skipped > 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl Khodrochi repair shops (listing + detail) into JSONL.",
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
        help="Stop listing phase after N pages (testing)",
    )
    parser.add_argument(
        "--limit-details",
        type=int,
        default=None,
        metavar="N",
        help="Stop detail phase after N successful writes (testing)",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=1.0,
        help="Minimum randomized delay between detail requests (seconds)",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=2.5,
        help="Maximum randomized delay between detail requests (seconds)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="HTTP retries per URL on transient failures",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip item_ids already present in the output JSONL",
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

    crawler = KhodrochiCrawler(
        output_path=args.output,
        limit_pages=args.limit_pages,
        limit_details=args.limit_details,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        max_retries=args.max_retries,
        timeout=args.timeout,
        resume=args.resume,
    )
    return crawler.run()


if __name__ == "__main__":
    sys.exit(main())
