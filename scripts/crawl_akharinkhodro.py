#!/usr/bin/env python3
"""
Crawler: akharinkhodro.ir car-spec articles (/category/cars/).

Strategy (from probe_akharinkhodro.py + production fix):
  - Parent category id=72 has REST count=0; posts live on child brand categories.
  - Crawl **one brand category at a time**
    GET /wp-json/wp/v2/posts?categories=<brand_id>&per_page=100
    (OR-ing all brand ids in one query can 500 on later pages.)
  - Parse HTML tables inside content.rendered into a flat ``specs`` map.
  - HTML archive pagination under-counts; do not use it for production.

Usage:
  python scripts/crawl_akharinkhodro.py
  python scripts/crawl_akharinkhodro.py --limit 5 -v
  python scripts/crawl_akharinkhodro.py --limit-pages 1 --output data/raw_akharinkhodro_cars_smoke.json
  docker exec seenad_backend python3 scripts/crawl_akharinkhodro.py -v

Full run (nohup):
  nohup docker exec seenad_backend python3 scripts/crawl_akharinkhodro.py -v \\
    > data/akharinkhodro_crawl.log 2>&1 &

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
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("akharinkhodro-cars-crawler")

BASE_URL = "https://akharinkhodro.ir"
CARS_CATEGORY_ID = 72
POSTS_ENDPOINT = f"{BASE_URL}/wp-json/wp/v2/posts"
CATEGORIES_ENDPOINT = f"{BASE_URL}/wp-json/wp/v2/categories"
DEFAULT_OUTPUT = Path("data/raw_akharinkhodro_cars.json")
SOURCE_NAME = "akharinkhodro.ir"
REQUEST_TIMEOUT_S = 60.0
PER_PAGE = 100
DEFAULT_DELAY_MIN_S = 0.4
DEFAULT_DELAY_MAX_S = 1.2
MAX_RETRIES = 4

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Referer": f"{BASE_URL}/category/cars/",
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
CAR_PATH_RE = re.compile(r"^/cars/([^/]+)/([^/]+)/?$", re.IGNORECASE)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_farsi_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return clean_farsi_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def sleep_polite(delay_min: float, delay_max: float) -> None:
    if delay_max <= 0:
        return
    low = min(delay_min, delay_max)
    high = max(delay_min, delay_max)
    time.sleep(random.uniform(low, high))


def parse_spec_tables(content_html: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Return (flat specs map, raw table rows) from article content HTML."""
    soup = BeautifulSoup(content_html or "", "html.parser")
    specs: dict[str, str] = {}
    tables_raw: list[dict[str, Any]] = []

    for index, table in enumerate(soup.find_all("table")):
        rows_out: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                clean_farsi_text(c.get_text(" ", strip=True)) or ""
                for c in tr.find_all(["th", "td"])
            ]
            if not any(cells):
                continue
            rows_out.append(cells)
            if len(cells) >= 2 and cells[0]:
                # First occurrence wins; later tables may repeat section headers.
                specs.setdefault(cells[0], cells[1])
        tables_raw.append({"index": index, "rows": rows_out})

    return specs, tables_raw


def brand_from_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    path = urlparse(url).path or ""
    match = CAR_PATH_RE.match(path)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def extract_brand_name(
    *,
    specs: dict[str, str],
    embedded_terms: list[dict[str, Any]],
    brand_slug: str | None,
    brand_id_to_name: dict[int, str],
    category_ids: list[int],
) -> str | None:
    if specs.get("برند"):
        return specs["برند"]
    for term in embedded_terms:
        if term.get("taxonomy") != "category":
            continue
        term_id = term.get("id")
        if isinstance(term_id, int) and term_id in brand_id_to_name:
            return brand_id_to_name[term_id]
        # child brand cats often appear in embed even without parent map hit
        slug = term.get("slug")
        if brand_slug and slug == brand_slug:
            return clean_farsi_text(term.get("name"))
    for cat_id in category_ids:
        if cat_id in brand_id_to_name:
            return brand_id_to_name[cat_id]
    return None


class AkharinKhodroCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        limit: int | None,
        limit_pages: int | None,
        delay_min: float,
        delay_max: float,
        per_page: int = PER_PAGE,
    ) -> None:
        self.output_path = output_path
        self.limit = limit
        self.limit_pages = limit_pages
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.per_page = max(1, min(per_page, 100))
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
                headers = {k.lower(): v for k, v in response.headers.items()}
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = min(2.0 * attempt, 10.0)
                    LOGGER.warning(
                        "HTTP %s on %s attempt=%s/%s — sleep %.1fs",
                        response.status_code,
                        url,
                        attempt,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json(), headers
            except (Timeout, RequestException, ValueError) as exc:
                last_error = exc
                wait = min(2.0 * attempt, 10.0)
                LOGGER.warning(
                    "Request failed url=%s attempt=%s/%s error=%s — sleep %.1fs",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"GET failed after {MAX_RETRIES} attempts: {url}") from last_error

    def fetch_brand_categories(self) -> list[dict[str, Any]]:
        LOGGER.info("Fetching child brand categories of parent id=%s", CARS_CATEGORY_ID)
        page = 1
        brands: list[dict[str, Any]] = []
        while True:
            payload, headers = self._get_json(
                CATEGORIES_ENDPOINT,
                params={
                    "parent": CARS_CATEGORY_ID,
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected categories payload type: {type(payload)}")
            brands.extend(payload)
            total_pages = int(headers.get("x-wp-totalpages") or page)
            LOGGER.info(
                "Brand categories page=%s returned=%s total_pages=%s",
                page,
                len(payload),
                total_pages,
            )
            if page >= total_pages or not payload:
                break
            page += 1
            sleep_polite(self.delay_min, self.delay_max)
        LOGGER.info("Loaded %s brand categories", len(brands))
        return brands

    def fetch_posts_page(
        self,
        *,
        category_id: int,
        page: int,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        params = {
            "categories": category_id,
            "per_page": self.per_page,
            "page": page,
            "_embed": "1",
        }
        payload, headers = self._get_json(POSTS_ENDPOINT, params=params)
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected posts payload type: {type(payload)}")
        return payload, headers

    def normalize_post(
        self,
        post: dict[str, Any],
        *,
        brand_id_to_name: dict[int, str],
        crawled_at: str,
    ) -> dict[str, Any]:
        title = html_to_text((post.get("title") or {}).get("rendered"))
        excerpt = html_to_text((post.get("excerpt") or {}).get("rendered"))
        content_html = (post.get("content") or {}).get("rendered") or ""
        content_text = html_to_text(content_html)
        specs, tables_raw = parse_spec_tables(content_html)

        url = post.get("link")
        brand_slug, model_slug = brand_from_url(url)

        embedded = post.get("_embedded") or {}
        featured = None
        media_list = embedded.get("wp:featuredmedia") or []
        if media_list and isinstance(media_list[0], dict):
            featured = media_list[0].get("source_url")

        term_names: list[str] = []
        embedded_terms: list[dict[str, Any]] = []
        for group in embedded.get("wp:term") or []:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                embedded_terms.append(term)
                name = clean_farsi_text(term.get("name"))
                if name:
                    term_names.append(name)

        category_ids = [int(x) for x in (post.get("categories") or []) if str(x).isdigit()]
        brand_name = extract_brand_name(
            specs=specs,
            embedded_terms=embedded_terms,
            brand_slug=brand_slug,
            brand_id_to_name=brand_id_to_name,
            category_ids=category_ids,
        )

        return {
            "id": post.get("id"),
            "url": url,
            "slug": post.get("slug"),
            "title": title,
            "date": post.get("date"),
            "modified": post.get("modified"),
            "brand": brand_name,
            "brand_slug": brand_slug,
            "model_slug": model_slug,
            "categories": category_ids,
            "tags": post.get("tags") or [],
            "term_names": term_names,
            "excerpt": excerpt,
            "featured_image": featured,
            "content_html": content_html,
            "content_text": content_text,
            "specs": specs,
            "source": SOURCE_NAME,
            "source_category_url": f"{BASE_URL}/category/cars/",
            "crawled_at": crawled_at,
            "raw_payload": {
                "spec_tables": tables_raw,
                "yoast_title": (post.get("yoast_head_json") or {}).get("title")
                if isinstance(post.get("yoast_head_json"), dict)
                else None,
            },
        }

    def run(self) -> dict[str, Any]:
        crawled_at = _now_iso()
        brands = self.fetch_brand_categories()
        if not brands:
            raise RuntimeError("No child brand categories found under cars (id=72)")

        brand_id_to_name = {
            int(b["id"]): clean_farsi_text(b.get("name")) or str(b.get("slug"))
            for b in brands
            if b.get("id") is not None
        }

        records: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        pages_fetched = 0
        total_reported = 0
        brands_ok = 0
        brands_failed = 0

        def ingest(posts: list[dict[str, Any]]) -> bool:
            """Append normalized posts. Returns False if limit reached."""
            for post in posts:
                post_id = post.get("id")
                if isinstance(post_id, int):
                    if post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)
                record = self.normalize_post(
                    post,
                    brand_id_to_name=brand_id_to_name,
                    crawled_at=crawled_at,
                )
                records.append(record)
                if self.limit is not None and len(records) >= self.limit:
                    return False
            return True

        for index, brand in enumerate(brands, start=1):
            brand_id = brand.get("id")
            if brand_id is None:
                continue
            brand_id = int(brand_id)
            brand_name = clean_farsi_text(brand.get("name")) or brand.get("slug")
            LOGGER.info(
                "[%s/%s] Brand id=%s name=%s count=%s",
                index,
                len(brands),
                brand_id,
                brand_name,
                brand.get("count"),
            )

            try:
                sleep_polite(self.delay_min, self.delay_max)
                first_page, headers = self.fetch_posts_page(category_id=brand_id, page=1)
                brand_total = int(headers.get("x-wp-total") or 0)
                brand_pages = int(headers.get("x-wp-totalpages") or 1)
                if self.limit_pages is not None:
                    brand_pages = min(brand_pages, max(1, self.limit_pages))
                total_reported += brand_total
                pages_fetched += 1
                LOGGER.info(
                    "  REST brand total=%s pages=%s first_page=%s",
                    brand_total,
                    brand_pages,
                    len(first_page),
                )

                if not ingest(first_page):
                    brands_ok += 1
                    return self._finalize(
                        records,
                        brands=brands,
                        total_reported=total_reported,
                        pages_fetched=pages_fetched,
                        crawled_at=crawled_at,
                        brands_ok=brands_ok,
                        brands_failed=brands_failed,
                    )

                for page in range(2, brand_pages + 1):
                    sleep_polite(self.delay_min, self.delay_max)
                    posts, _ = self.fetch_posts_page(category_id=brand_id, page=page)
                    pages_fetched += 1
                    LOGGER.info(
                        "  page=%s/%s count=%s",
                        page,
                        brand_pages,
                        len(posts),
                    )
                    if not posts:
                        LOGGER.warning("  empty page %s — stop brand pagination", page)
                        break
                    if not ingest(posts):
                        brands_ok += 1
                        return self._finalize(
                            records,
                            brands=brands,
                            total_reported=total_reported,
                            pages_fetched=pages_fetched,
                            crawled_at=crawled_at,
                            brands_ok=brands_ok,
                            brands_failed=brands_failed,
                        )
                brands_ok += 1
            except Exception as exc:
                brands_failed += 1
                LOGGER.warning("  brand failed id=%s name=%s error=%s", brand_id, brand_name, exc)
                continue

        return self._finalize(
            records,
            brands=brands,
            total_reported=total_reported,
            pages_fetched=pages_fetched,
            crawled_at=crawled_at,
            brands_ok=brands_ok,
            brands_failed=brands_failed,
        )

    def _finalize(
        self,
        records: list[dict[str, Any]],
        *,
        brands: list[dict[str, Any]],
        total_reported: int,
        pages_fetched: int,
        crawled_at: str,
        brands_ok: int = 0,
        brands_failed: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "crawl_metadata": {
                "source": SOURCE_NAME,
                "source_category_url": f"{BASE_URL}/category/cars/",
                "parent_category_id": CARS_CATEGORY_ID,
                "strategy": "wordpress_rest_per_brand_category",
                "crawled_at": crawled_at,
                "brand_category_count": len(brands),
                "brands_ok": brands_ok,
                "brands_failed": brands_failed,
                "rest_total_reported": total_reported,
                "pages_fetched": pages_fetched,
                "per_page": self.per_page,
                "record_count": len(records),
                "unique_ids": len({r.get("id") for r in records if r.get("id") is not None}),
                "limit": self.limit,
                "limit_pages": self.limit_pages,
            },
            "brands": [
                {
                    "id": b.get("id"),
                    "name": clean_farsi_text(b.get("name")),
                    "slug": b.get("slug"),
                    "count": b.get("count"),
                    "link": b.get("link"),
                }
                for b in brands
            ],
            "articles": records,
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        self.output_path.write_text(serialized, encoding="utf-8")
        LOGGER.info(
            "Wrote %s articles → %s (%s bytes) brands_ok=%s brands_failed=%s",
            len(records),
            self.output_path.resolve(),
            len(serialized.encode("utf-8")),
            brands_ok,
            brands_failed,
        )
        return payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl akharinkhodro.ir car-spec articles via WordPress REST "
            "into data/raw_akharinkhodro_cars.json."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSON output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N articles (smoke tests).",
    )
    parser.add_argument(
        "--limit-pages",
        type=int,
        default=None,
        help="Fetch at most N REST pages per brand (per_page=100).",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=PER_PAGE,
        help=f"WordPress per_page (max 100, default: {PER_PAGE})",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=DEFAULT_DELAY_MIN_S,
        help=f"Min polite delay seconds (default: {DEFAULT_DELAY_MIN_S})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=DEFAULT_DELAY_MAX_S,
        help=f"Max polite delay seconds (default: {DEFAULT_DELAY_MAX_S})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)

    crawler = AkharinKhodroCrawler(
        output_path=args.output,
        limit=args.limit,
        limit_pages=args.limit_pages,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        per_page=args.per_page,
    )
    try:
        payload = crawler.run()
    except Exception as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 2

    count = len(payload.get("articles") or [])
    if count == 0:
        LOGGER.error("No articles crawled")
        return 1
    LOGGER.info("Done. articles=%s output=%s", count, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
