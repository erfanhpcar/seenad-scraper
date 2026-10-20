#!/usr/bin/env python3
"""
Crawler: khodrobank.com car specs catalog (/cars).

Strategy (from probe_khodrobank.py):
  1. GET /cars → a.brand_list_item brands (~117)
  2. GET each brand page → a.carspecs__item model URLs
  3. GET each car detail → schema.org Car JSON-LD + topspecs
     + div "table" (#kb-spec-car .kb-tcl-hoder) + fuel/price lists
  4. Append JSONL rows (resumeable), then compile a pretty JSON file

Detail pages do **not** use HTML <table> for specs. Technical fields live in
``.kb-tcl-hoder`` (right=label, left=value). Fuel and price/dealership are
paired ``.compare_single`` lists.

Usage:
  python scripts/crawl_khodrobank.py --url https://www.khodrobank.com/cars/JMC/449/JMC-S350 -v
  python scripts/crawl_khodrobank.py --limit 5 -v
  python scripts/crawl_khodrobank.py --limit-brands 2 --output data/raw_khodrobank_cars_smoke.jsonl
  python scripts/crawl_khodrobank.py --resume
  docker exec -i seenad_backend python3 scripts/crawl_khodrobank.py --limit 5 -v

Full run (later / nohup):
  nohup docker exec -i seenad_backend python3 scripts/crawl_khodrobank.py --resume -v \\
    > data/khodrobank_crawl.log 2>&1 &

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
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("khodrobank-cars-crawler")

BASE_URL = "https://www.khodrobank.com"
LISTING_URL = f"{BASE_URL}/cars"
DEFAULT_OUTPUT = Path("data/raw_khodrobank_cars.jsonl")
DEFAULT_JSON_OUTPUT = Path("data/raw_khodrobank_cars.json")
SOURCE_NAME = "khodrobank.com"
REQUEST_TIMEOUT_S = 75.0
DEFAULT_DELAY_MIN_S = 0.6
DEFAULT_DELAY_MAX_S = 1.6
MAX_RETRIES = 4

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
CAR_URL_RE = re.compile(r"/cars/([^/]+)/(\d+)/([^/]+)/?$", re.IGNORECASE)
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


def parse_car_url(url: str) -> dict[str, Any]:
    path = unquote(urlparse(url).path or "")
    match = CAR_URL_RE.search(path)
    if not match:
        return {"brand_slug": None, "car_id": None, "model_slug": None}
    return {
        "brand_slug": match.group(1),
        "car_id": int(match.group(2)),
        "model_slug": match.group(3),
    }


def parse_ld_json_objects(soup: BeautifulSoup) -> list[Any]:
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


def extract_brands(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict[str, Any]] = []
    for anchor in soup.select("a.brand_list_item"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        name_el = anchor.select_one(".brand_item_name_fa")
        name = clean_text(
            name_el.get_text(" ", strip=True) if name_el else anchor.get_text(" ", strip=True)
        )
        brands.append(
            {
                "name": name,
                "url": url,
                "slug": unquote(urlparse(url).path.rstrip("/").split("/")[-1]),
            }
        )
    return brands


def extract_brand_cars(html: str, *, brand_slug: str | None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cars: list[dict[str, Any]] = []
    seen: set[int] = set()
    for anchor in soup.select("a.carspecs__item"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        parsed = parse_car_url(url)
        car_id = parsed.get("car_id")
        if not isinstance(car_id, int):
            continue
        if brand_slug and parsed.get("brand_slug") and parsed["brand_slug"] != brand_slug:
            continue
        if car_id in seen:
            continue
        seen.add(car_id)
        title_el = anchor.select_one("h5.carspecs__title")
        year_el = anchor.select_one(".carspecs__year")
        cars.append(
            {
                "url": url,
                "title": clean_text(title_el.get_text(" ", strip=True) if title_el else None),
                "year_label": clean_text(year_el.get_text(" ", strip=True) if year_el else None),
                **parsed,
            }
        )
    return cars


def extract_topspecs(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for item in soup.select(".car-single_topspecs_item"):
        chunks = [clean_text(x) for x in item.stripped_strings]
        chunks = [c for c in chunks if c]
        if len(chunks) >= 2:
            specs[chunks[0]] = chunks[1]
    return specs


def extract_kb_tcl_specs(soup: BeautifulSoup) -> dict[str, str]:
    """Key/value rows in #kb-spec-car and the امکانات block (not HTML tables)."""
    specs: dict[str, str] = {}
    for item in soup.select(".kb-tcl-hoder"):
        right = item.select_one(".kb-tcl-right")
        left = item.select_one(".kb-tcl-left")
        key = clean_text(right.get_text(" ", strip=True) if right else None)
        value = clean_text(left.get_text(" ", strip=True) if left else None)
        if key:
            specs.setdefault(key, value or "")
    return specs


def extract_compare_blocks(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Paired title/value lists: fuel economy, then price + dealership."""
    blocks: list[dict[str, str]] = []
    for block in soup.select(".compare_single"):
        titles = [
            clean_text(li.get_text(" ", strip=True)) or ""
            for li in block.select(".compare_single_titles li")
        ]
        values = [
            clean_text(li.get_text(" ", strip=True)) or ""
            for li in block.select(".compare_single_specs li")
        ]
        paired: dict[str, str] = {}
        for title, value in zip(titles, values):
            if title:
                paired[title] = value
        if paired:
            blocks.append(paired)
    return blocks


def split_compare_blocks(
    blocks: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    fuel: dict[str, str] = {}
    price: dict[str, str] = {}
    for block in blocks:
        keys = set(block)
        if keys & {"قیمت", "نمایندگی"}:
            price.update(block)
        else:
            fuel.update(block)
    return fuel, price


def extract_specs_table(soup: BeautifulSoup) -> dict[str, str]:
    """Fallback for rare HTML <table> spec pages (comparison tables skipped)."""
    specs: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = [
            clean_text(c.get_text(" ", strip=True)) or ""
            for c in rows[0].find_all(["th", "td"])
        ]
        if len(first_cells) > 2 or (first_cells and first_cells[0] == "نام ماشین"):
            continue
        for tr in rows:
            cells = [
                clean_text(c.get_text(" ", strip=True)) or ""
                for c in tr.find_all(["th", "td"])
            ]
            if len(cells) >= 2 and cells[0]:
                specs.setdefault(cells[0], cells[1])
        if specs:
            break
    return specs


def extract_list_texts(soup: BeautifulSoup, selector: str) -> list[str]:
    items: list[str] = []
    for el in soup.select(selector):
        text = clean_text(el.get_text(" ", strip=True))
        if text:
            items.append(text)
    return items


def absolute_image(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return urljoin(BASE_URL, url)


class KhodroBankCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        json_output_path: Path | None,
        resume: bool,
        limit: int | None,
        limit_brands: int | None,
        delay_min: float,
        delay_max: float,
        urls: list[str] | None = None,
    ) -> None:
        self.output_path = output_path
        self.json_output_path = json_output_path
        self.resume = resume
        self.limit = limit
        self.limit_brands = limit_brands
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.urls = [u.strip() for u in (urls or []) if u.strip()]
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._done_ids: set[int] = set()

    def _load_resume_ids(self) -> None:
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
                car_id = row.get("car_id")
                if isinstance(car_id, int):
                    self._done_ids.add(car_id)
        LOGGER.info("Resume: loaded %s existing car_id(s)", len(self._done_ids))

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

    def normalize_detail(
        self,
        html: str,
        *,
        url: str,
        listing_title: str | None,
        year_label: str | None,
        brand_name_hint: str | None,
        crawled_at: str,
    ) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        parsed = parse_car_url(url)
        ld_objects = parse_ld_json_objects(soup)
        car_ld = next(
            (o for o in ld_objects if isinstance(o, dict) and o.get("@type") == "Car"),
            None,
        )
        topspecs = extract_topspecs(soup)
        kb_tcl_specs = extract_kb_tcl_specs(soup)
        table_specs = extract_specs_table(soup)
        fuel_specs, price_block = split_compare_blocks(extract_compare_blocks(soup))
        specs = {**topspecs, **table_specs, **kb_tcl_specs, **fuel_specs}

        brand = brand_name_hint
        name = None
        model = None
        release_date = None
        image = None
        description = None
        rating_value = None
        rating_count = None
        acceleration = None
        review_dates: dict[str, Any] = {}

        if isinstance(car_ld, dict):
            name = clean_text(car_ld.get("name"))
            model = clean_text(car_ld.get("model"))
            release_date = clean_text(
                str(car_ld.get("releaseDate") or car_ld.get("vehicleModelDate") or "")
            ) or None
            image = absolute_image(car_ld.get("image") if isinstance(car_ld.get("image"), str) else None)
            description = clean_text(
                BeautifulSoup(car_ld.get("description") or "", "html.parser").get_text(" ", strip=True)
            )
            if isinstance(car_ld.get("brand"), dict):
                brand = clean_text(car_ld["brand"].get("name")) or brand
            agg = car_ld.get("aggregateRating")
            if isinstance(agg, dict):
                rating_value = agg.get("ratingValue")
                rating_count = agg.get("ratingCount")
            accel = car_ld.get("accelerationTime")
            if isinstance(accel, dict):
                acceleration = {
                    "value": accel.get("value"),
                    "unit": accel.get("unitCode"),
                }
            review = car_ld.get("review")
            if isinstance(review, dict):
                review_dates = {
                    "date_published": review.get("datePublished"),
                    "date_modified": review.get("dateModified"),
                }

        return {
            "car_id": parsed.get("car_id"),
            "url": url,
            "brand": brand,
            "brand_slug": parsed.get("brand_slug"),
            "model_slug": parsed.get("model_slug"),
            "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
            "listing_title": listing_title,
            "year_label": year_label,
            "name": name,
            "model": model,
            "release_date": release_date,
            "image": image,
            "description": description,
            "rating_value": rating_value,
            "rating_count": rating_count,
            "acceleration": acceleration,
            "specs": specs,
            "fuel": fuel_specs,
            "price": price_block.get("قیمت"),
            "dealership": price_block.get("نمایندگی"),
            "pros": extract_list_texts(soup, ".car-single_pros_item"),
            "cons": extract_list_texts(soup, ".car-single_cons_item"),
            "source": SOURCE_NAME,
            "source_listing_url": LISTING_URL,
            "crawled_at": crawled_at,
            "raw_payload": {
                "topspecs": topspecs,
                "kb_tcl_specs": kb_tcl_specs,
                "specs_table": table_specs,
                "fuel": fuel_specs,
                "price_block": price_block,
                "review_dates": review_dates,
                "car_json_ld_keys": sorted(car_ld.keys()) if isinstance(car_ld, dict) else [],
                "html_bytes": len(html),
            },
        }

    def _write_json_bundle(self, *, crawled_at: str, extra_meta: dict[str, Any] | None = None) -> None:
        if self.json_output_path is None:
            return
        records: list[dict[str, Any]] = []
        if self.output_path.exists():
            with self.output_path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        LOGGER.warning("Skip bad JSONL line %s while compiling JSON", line_no)
        payload = {
            "crawl_metadata": {
                "source": SOURCE_NAME,
                "source_listing_url": LISTING_URL,
                "strategy": "html_brands_then_kb_tcl_specs",
                "crawled_at": crawled_at,
                "record_count": len(records),
                "unique_ids": len({r.get("car_id") for r in records if r.get("car_id") is not None}),
                "jsonl_path": str(self.output_path),
                **(extra_meta or {}),
            },
            "cars": records,
        }
        self.json_output_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        self.json_output_path.write_text(serialized, encoding="utf-8")
        LOGGER.info(
            "Wrote JSON bundle cars=%s → %s (%s bytes)",
            len(records),
            self.json_output_path.resolve(),
            len(serialized.encode("utf-8")),
        )

    def _crawl_one_car(
        self,
        *,
        url: str,
        listing_title: str | None,
        year_label: str | None,
        brand_name_hint: str | None,
        crawled_at: str,
    ) -> dict[str, Any]:
        detail_html = self._get_html(url)
        record = self.normalize_detail(
            detail_html,
            url=url,
            listing_title=listing_title,
            year_label=year_label,
            brand_name_hint=brand_name_hint,
            crawled_at=crawled_at,
        )
        if not record.get("car_id"):
            raise RuntimeError("missing car_id")
        self._append_jsonl(record)
        self._done_ids.add(int(record["car_id"]))
        return record

    def run(self) -> int:
        crawled_at = _now_iso()
        self._load_resume_ids()
        if not self.resume and self.output_path.exists():
            LOGGER.info("Removing existing output (no --resume): %s", self.output_path)
            self.output_path.unlink()

        success = 0
        skipped = 0
        failed = 0
        extra_meta: dict[str, Any] = {"limit": self.limit, "limit_brands": self.limit_brands}

        if self.urls:
            extra_meta["mode"] = "explicit_urls"
            extra_meta["url_count"] = len(self.urls)
            for url in self.urls:
                parsed = parse_car_url(url)
                car_id = parsed.get("car_id")
                if isinstance(car_id, int) and car_id in self._done_ids:
                    skipped += 1
                    LOGGER.info("Skip already crawled car_id=%s %s", car_id, url)
                    continue
                if self.limit is not None and success >= self.limit:
                    break
                sleep_polite(self.delay_min, self.delay_max)
                try:
                    record = self._crawl_one_car(
                        url=url,
                        listing_title=None,
                        year_label=None,
                        brand_name_hint=None,
                        crawled_at=crawled_at,
                    )
                    success += 1
                    LOGGER.info(
                        "  + id=%s title=%s specs=%s",
                        record["car_id"],
                        record.get("name") or record.get("listing_title"),
                        len(record.get("specs") or {}),
                    )
                except Exception as exc:
                    failed += 1
                    LOGGER.warning("  ! failed %s: %s", url, exc)
            extra_meta.update({"success": success, "skipped": skipped, "failed": failed})
            self._write_json_bundle(crawled_at=crawled_at, extra_meta=extra_meta)
            LOGGER.info(
                "Done (urls). success=%s skipped=%s failed=%s output=%s",
                success,
                skipped,
                failed,
                self.output_path.resolve(),
            )
            return 0 if success or skipped else 1

        LOGGER.info("Fetching brand listing %s", LISTING_URL)
        listing_html = self._get_html(LISTING_URL)
        brands = extract_brands(listing_html)
        if self.limit_brands is not None:
            brands = brands[: max(0, self.limit_brands)]
        LOGGER.info("Brands to crawl: %s", len(brands))
        extra_meta["brands"] = len(brands)
        if not brands:
            LOGGER.error("No brands found")
            return 1

        reached_limit = False
        for brand_index, brand in enumerate(brands, start=1):
            sleep_polite(self.delay_min, self.delay_max)
            LOGGER.info(
                "[%s/%s] Brand %s (%s)",
                brand_index,
                len(brands),
                brand.get("name"),
                brand.get("url"),
            )
            try:
                brand_html = self._get_html(brand["url"])
            except Exception as exc:
                LOGGER.warning("Brand fetch failed %s: %s", brand.get("url"), exc)
                failed += 1
                continue

            cars = extract_brand_cars(brand_html, brand_slug=brand.get("slug"))
            LOGGER.info("  models found=%s", len(cars))

            for car in cars:
                car_id = car.get("car_id")
                if isinstance(car_id, int) and car_id in self._done_ids:
                    skipped += 1
                    continue
                if self.limit is not None and success >= self.limit:
                    LOGGER.info("Reached --limit=%s", self.limit)
                    reached_limit = True
                    break

                sleep_polite(self.delay_min, self.delay_max)
                try:
                    record = self._crawl_one_car(
                        url=car["url"],
                        listing_title=car.get("title"),
                        year_label=car.get("year_label"),
                        brand_name_hint=brand.get("name"),
                        crawled_at=crawled_at,
                    )
                    success += 1
                    LOGGER.info(
                        "  + id=%s title=%s specs=%s",
                        record["car_id"],
                        record.get("name") or record.get("listing_title"),
                        len(record.get("specs") or {}),
                    )
                except Exception as exc:
                    failed += 1
                    LOGGER.warning("  ! failed %s: %s", car.get("url"), exc)
            if reached_limit:
                break

        extra_meta.update(
            {
                "success": success,
                "skipped": skipped,
                "failed": failed,
                "reached_limit": reached_limit,
            }
        )
        self._write_json_bundle(crawled_at=crawled_at, extra_meta=extra_meta)
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
    parser = argparse.ArgumentParser(
        description="Crawl khodrobank.com car specs into JSONL + JSON.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help=(
            "Pretty JSON bundle path (cars + crawl_metadata). "
            f"Default: same stem as --output with .json ({DEFAULT_JSON_OUTPUT})"
        ),
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip compiling the pretty JSON bundle.",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Crawl only this car detail URL (repeatable). Skips brand listing.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max car details to fetch")
    parser.add_argument("--limit-brands", type=int, default=None, help="Max brands to visit")
    parser.add_argument("--resume", action="store_true", help="Skip car_ids already in output JSONL")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN_S)
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX_S)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)
    json_output = None if args.no_json else (args.json_output or args.output.with_suffix(".json"))
    crawler = KhodroBankCrawler(
        output_path=args.output,
        json_output_path=json_output,
        resume=args.resume,
        limit=args.limit,
        limit_brands=args.limit_brands,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        urls=args.url,
    )
    try:
        return crawler.run()
    except Exception as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
