#!/usr/bin/env python3
"""
Crawler: asbe-bokhar.com car specs bank (/cars/).

Strategy (from probe_asbebokhar.py):
  1. GET /cars/ → brand links (~54)
  2. Paginate each /cars/{brand}/ archive → model URLs
  3. GET /cars/{brand}/{model}/ → icon specs (#single-spec) + HTML table + Article JSON-LD
  4. Append JSONL (resumeable). WP REST is 401 without login.

Usage:
  python scripts/crawl_asbebokhar.py --limit 5 -v
  python scripts/crawl_asbebokhar.py --limit-brands 2 --output data/raw_asbebokhar_cars_smoke.jsonl
  python scripts/crawl_asbebokhar.py --resume
  docker exec -i seenad_backend python3 scripts/crawl_asbebokhar.py --limit 5 -v

Full run (later / nohup):
  nohup docker exec -i seenad_backend python3 scripts/crawl_asbebokhar.py --resume -v \\
    > data/asbebokhar_crawl.log 2>&1 &

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("asbebokhar-cars-crawler")

BASE_URL = "https://asbe-bokhar.com"
LISTING_URL = f"{BASE_URL}/cars/"
DEFAULT_OUTPUT = Path("data/raw_asbebokhar_cars.jsonl")
SOURCE_NAME = "asbe-bokhar.com"
REQUEST_TIMEOUT_S = 75.0
DEFAULT_DELAY_MIN_S = 0.5
DEFAULT_DELAY_MAX_S = 1.4
MAX_RETRIES = 4
MAX_BRAND_PAGES = 30

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Referer": LISTING_URL,
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
BRAND_RE = re.compile(r"^https://asbe-bokhar\.com/cars/([^/]+)/?$")
ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "access denied",
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def sleep_polite(delay_min: float, delay_max: float) -> None:
    if delay_max <= 0:
        return
    low = min(delay_min, delay_max)
    high = max(delay_min, delay_max)
    time.sleep(random.uniform(low, high))


def is_blocked(status: int, html: str) -> bool:
    lowered = (html or "").lower()
    return status in (403, 429, 503) or any(m in lowered for m in ANTI_BOT_MARKERS)


def extract_brands(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, anchor["href"]).split("?")[0]
        normalized = href if href.endswith("/") else href + "/"
        match = BRAND_RE.match(normalized)
        if not match:
            continue
        slug = match.group(1)
        if slug in seen or slug == "page":
            continue
        name = clean_text(anchor.get_text(" ", strip=True))
        if not name or name == "مشخصات فنی":
            continue
        seen.add(slug)
        brands.append({"name": name, "slug": slug, "url": f"{BASE_URL}/cars/{slug}/"})
    return brands


def extract_brand_cars(html: str, *, brand_slug: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cars: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = re.compile(
        rf"^https://asbe-bokhar\.com/cars/{re.escape(brand_slug)}/([^/]+)/?$"
    )
    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, anchor["href"]).split("?")[0]
        normalized = href if href.endswith("/") else href + "/"
        match = pattern.match(normalized)
        if not match:
            continue
        model_slug = match.group(1)
        if model_slug in seen or model_slug == "page":
            continue
        seen.add(model_slug)
        img = anchor.find("img")
        title = None
        if img is not None:
            title = clean_text(img.get("alt") or img.get("title"))
        cars.append(
            {
                "url": f"{BASE_URL}/cars/{brand_slug}/{model_slug}/",
                "brand_slug": brand_slug,
                "model_slug": model_slug,
                "listing_title": title,
            }
        )
    return cars


def parse_ld_objects(soup: BeautifulSoup) -> list[Any]:
    objects: list[Any] = []
    decoder = json.JSONDecoder()
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            objects.append(json.loads(raw))
            continue
        except json.JSONDecodeError:
            pass
        idx = 0
        while idx < len(raw):
            while idx < len(raw) and raw[idx].isspace():
                idx += 1
            if idx >= len(raw):
                break
            try:
                obj, end = decoder.raw_decode(raw, idx)
                objects.append(obj)
                idx = end
            except json.JSONDecodeError:
                break
    return objects


def find_article_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for obj in parse_ld_objects(soup):
        if isinstance(obj, dict) and obj.get("@type") == "Article":
            return obj
        if isinstance(obj, dict) and "@graph" in obj:
            for node in obj["@graph"]:
                if isinstance(node, dict) and node.get("@type") == "Article":
                    return node
    return None


def extract_icon_specs(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for item in soup.select("#single-spec .single-spec, .single-spec"):
        img = item.find("img")
        label = None
        if img is not None:
            label = clean_text(img.get("title") or img.get("alt"))
        value_el = item.find("span")
        value = clean_text(value_el.get_text(" ", strip=True) if value_el else None)
        if label and value:
            specs[label] = value
    return specs


def extract_table_specs(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [
                clean_text(c.get_text(" ", strip=True)) or ""
                for c in tr.find_all(["th", "td"])
            ]
            if len(cells) >= 2 and cells[0]:
                specs.setdefault(cells[0], cells[1])
    return specs


def extract_content_text(soup: BeautifulSoup) -> str | None:
    for sel in (
        ".td-post-content",
        ".entry-content",
        ".post-content",
        "article .content",
        ".pf-content",
    ):
        el = soup.select_one(sel)
        if el:
            return clean_text(el.get_text(" ", strip=True))
    return None


class AsbeBokharCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        resume: bool,
        limit: int | None,
        limit_brands: int | None,
        delay_min: float,
        delay_max: float,
    ) -> None:
        self.output_path = output_path
        self.resume = resume
        self.limit = limit
        self.limit_brands = limit_brands
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._done_keys: set[str] = set()

    @staticmethod
    def record_key(brand_slug: str | None, model_slug: str | None) -> str:
        return f"{brand_slug}/{model_slug}"

    def _load_resume_keys(self) -> None:
        if not self.resume or not self.output_path.exists():
            return
        with self.output_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Skip bad JSONL line %s", line_no)
                    continue
                key = self.record_key(row.get("brand_slug"), row.get("model_slug"))
                if row.get("brand_slug") and row.get("model_slug"):
                    self._done_keys.add(key)
        LOGGER.info("Resume: loaded %s existing records", len(self._done_keys))

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _get_html(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_S)
                response.encoding = response.encoding or "utf-8"
                html = response.text
                if is_blocked(response.status_code, html):
                    raise RuntimeError(f"Anti-bot/blocked HTTP {response.status_code} for {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = min(2.0 * attempt, 12.0)
                    LOGGER.warning(
                        "HTTP %s url=%s attempt=%s/%s — sleep %.1fs",
                        response.status_code,
                        url,
                        attempt,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return html
            except (Timeout, RequestException, RuntimeError) as exc:
                last_error = exc
                wait = min(2.0 * attempt, 12.0)
                LOGGER.warning(
                    "Fetch failed url=%s attempt=%s/%s error=%s — sleep %.1fs",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"GET failed after {MAX_RETRIES} attempts: {url}") from last_error

    def collect_brand_cars(self, brand: dict[str, Any]) -> list[dict[str, Any]]:
        url = brand["url"]
        cars: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        while url and pages < MAX_BRAND_PAGES:
            pages += 1
            sleep_polite(self.delay_min, self.delay_max)
            html = self._get_html(url)
            page_cars = extract_brand_cars(html, brand_slug=brand["slug"])
            for car in page_cars:
                if car["model_slug"] in seen:
                    continue
                seen.add(car["model_slug"])
                cars.append(car)
            nxt = (BeautifulSoup(html, "html.parser").find("link", rel="next") or {}).get("href")
            url = nxt
        LOGGER.info("  brand pages=%s models=%s", pages, len(cars))
        return cars

    def normalize_detail(
        self,
        html: str,
        *,
        url: str,
        brand_name: str | None,
        listing_title: str | None,
        brand_slug: str,
        model_slug: str,
        crawled_at: str,
    ) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        article = find_article_ld(soup)
        icon_specs = extract_icon_specs(soup)
        table_specs = extract_table_specs(soup)
        specs = {**icon_specs, **table_specs}

        image = None
        author = None
        if article:
            image_obj = article.get("image")
            if isinstance(image_obj, dict):
                image = image_obj.get("url")
            elif isinstance(image_obj, str):
                image = image_obj
            elif isinstance(image_obj, list) and image_obj:
                first = image_obj[0]
                image = first.get("url") if isinstance(first, dict) else first
            auth = article.get("author")
            if isinstance(auth, dict):
                author = clean_text(auth.get("name"))
            elif isinstance(auth, list) and auth and isinstance(auth[0], dict):
                author = clean_text(auth[0].get("name"))

        return {
            "url": url,
            "brand": brand_name,
            "brand_slug": brand_slug,
            "model_slug": model_slug,
            "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
            "headline": clean_text(article.get("headline")) if article else None,
            "listing_title": listing_title,
            "date_published": article.get("datePublished") if article else None,
            "date_modified": article.get("dateModified") if article else None,
            "author": author,
            "image": image,
            "specs": specs,
            "content_text": extract_content_text(soup),
            "source": SOURCE_NAME,
            "source_listing_url": LISTING_URL,
            "crawled_at": crawled_at,
            "raw_payload": {
                "icon_specs": icon_specs,
                "table_specs": table_specs,
                "html_bytes": len(html),
            },
        }

    def run(self) -> int:
        crawled_at = _now_iso()
        self._load_resume_keys()
        if not self.resume and self.output_path.exists():
            LOGGER.info("Removing existing output (no --resume): %s", self.output_path)
            self.output_path.unlink()

        LOGGER.info("Fetching brand listing %s", LISTING_URL)
        listing_html = self._get_html(LISTING_URL)
        brands = extract_brands(listing_html)
        if self.limit_brands is not None:
            brands = brands[: max(0, self.limit_brands)]
        LOGGER.info("Brands to crawl: %s", len(brands))
        if not brands:
            LOGGER.error("No brands found")
            return 1

        success = 0
        skipped = 0
        failed = 0

        for brand_index, brand in enumerate(brands, start=1):
            LOGGER.info(
                "[%s/%s] Brand %s (%s)",
                brand_index,
                len(brands),
                brand.get("name"),
                brand.get("url"),
            )
            try:
                cars = self.collect_brand_cars(brand)
            except Exception as exc:
                LOGGER.warning("Brand failed %s: %s", brand.get("url"), exc)
                failed += 1
                continue

            for car in cars:
                key = self.record_key(car.get("brand_slug"), car.get("model_slug"))
                if key in self._done_keys:
                    skipped += 1
                    continue
                if self.limit is not None and success >= self.limit:
                    LOGGER.info("Reached --limit=%s", self.limit)
                    LOGGER.info(
                        "Done early. success=%s skipped=%s failed=%s output=%s",
                        success,
                        skipped,
                        failed,
                        self.output_path,
                    )
                    return 0 if success or skipped else 1

                sleep_polite(self.delay_min, self.delay_max)
                try:
                    detail_html = self._get_html(car["url"])
                    record = self.normalize_detail(
                        detail_html,
                        url=car["url"],
                        brand_name=brand.get("name"),
                        listing_title=car.get("listing_title"),
                        brand_slug=car["brand_slug"],
                        model_slug=car["model_slug"],
                        crawled_at=crawled_at,
                    )
                    self._append_jsonl(record)
                    self._done_keys.add(key)
                    success += 1
                    LOGGER.info(
                        "  + %s specs=%s title=%s",
                        key,
                        len(record.get("specs") or {}),
                        (record.get("headline") or record.get("listing_title") or "")[:70],
                    )
                except Exception as exc:
                    failed += 1
                    LOGGER.warning("  ! failed %s: %s", car.get("url"), exc)

        LOGGER.info(
            "Done. success=%s skipped_resume=%s failed=%s output=%s",
            success,
            skipped,
            failed,
            self.output_path.resolve(),
        )
        if success == 0 and skipped == 0:
            return 1
        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl asbe-bokhar.com car specs into JSONL.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--limit-brands", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN_S)
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX_S)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)
    crawler = AsbeBokharCrawler(
        output_path=args.output,
        resume=args.resume,
        limit=args.limit,
        limit_brands=args.limit_brands,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )
    try:
        return crawler.run()
    except Exception as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
