#!/usr/bin/env python3
"""
Crawler: khodro45.com magazine articles — category review-compare (بررسی و مقایسه).

Strategy (from probe_khodro45_articles.py):
  - REST: GET /mag/wp-json/wp/v2/posts?categories=5&per_page=100
  - Category slug=review-compare (~1543 articles)
  - Store full content_html + content_text as JSONL (resumeable)

Usage:
  python scripts/crawl_khodro45_articles.py --limit 5 -v
  python scripts/crawl_khodro45_articles.py --limit-pages 1 \\
      --output data/raw_khodro45_review_compare_smoke.jsonl
  python scripts/crawl_khodro45_articles.py --resume
  docker exec -i seenad_backend python3 scripts/crawl_khodro45_articles.py --limit 5 -v

Full run (later / nohup):
  nohup docker exec -i seenad_backend python3 scripts/crawl_khodro45_articles.py \\
    --resume -v > data/khodro45_articles_crawl.log 2>&1 &

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

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("khodro45-articles-crawler")

BASE_URL = "https://khodro45.com"
LISTING_URL = f"{BASE_URL}/mag/category/review-compare/"
API_BASE = f"{BASE_URL}/mag/wp-json/wp/v2"
CATEGORY_SLUG = "review-compare"
DEFAULT_OUTPUT = Path("data/raw_khodro45_review_compare.jsonl")
SOURCE_NAME = "khodro45.com/mag"
REQUEST_TIMEOUT_S = 60.0
PER_PAGE = 100
DEFAULT_DELAY_MIN_S = 0.35
DEFAULT_DELAY_MAX_S = 1.0
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
    "Referer": LISTING_URL,
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")


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


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def sleep_polite(delay_min: float, delay_max: float) -> None:
    if delay_max <= 0:
        return
    low = min(delay_min, delay_max)
    high = max(delay_min, delay_max)
    time.sleep(random.uniform(low, high))


class Khodro45ArticlesCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        resume: bool,
        limit: int | None,
        limit_pages: int | None,
        delay_min: float,
        delay_max: float,
        per_page: int = PER_PAGE,
        category_id: int | None = None,
    ) -> None:
        self.output_path = output_path
        self.resume = resume
        self.limit = limit
        self.limit_pages = limit_pages
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.per_page = max(1, min(per_page, 100))
        self.category_id = category_id
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
                post_id = row.get("id")
                if isinstance(post_id, int):
                    self._done_ids.add(post_id)
        LOGGER.info("Resume: loaded %s existing id(s)", len(self._done_ids))

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

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

    def resolve_category_id(self) -> int:
        if self.category_id is not None:
            return self.category_id
        payload, _ = self._get_json(
            f"{API_BASE}/categories",
            params={"slug": CATEGORY_SLUG},
        )
        if not isinstance(payload, list) or not payload:
            raise RuntimeError(f"Category slug not found: {CATEGORY_SLUG}")
        return int(payload[0]["id"])

    def normalize_post(self, post: dict[str, Any], *, crawled_at: str) -> dict[str, Any]:
        content_html = (post.get("content") or {}).get("rendered") or ""
        excerpt_html = (post.get("excerpt") or {}).get("rendered") or ""
        title_html = (post.get("title") or {}).get("rendered") or ""

        featured = None
        author = None
        tags: list[str] = []
        categories: list[str] = []
        emb = post.get("_embedded") or {}
        media = emb.get("wp:featuredmedia") or []
        if media and isinstance(media[0], dict):
            featured = media[0].get("source_url")
        authors = emb.get("author") or []
        if authors and isinstance(authors[0], dict):
            author = clean_text(authors[0].get("name"))
        for group in emb.get("wp:term") or []:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                name = clean_text(term.get("name"))
                if not name:
                    continue
                tax = term.get("taxonomy")
                if tax == "post_tag":
                    tags.append(name)
                elif tax == "category":
                    categories.append(name)

        return {
            "id": post.get("id"),
            "url": post.get("link"),
            "slug": post.get("slug"),
            "title": html_to_text(title_html),
            "date": post.get("date"),
            "modified": post.get("modified"),
            "author": author,
            "categories": categories,
            "category_ids": post.get("categories") or [],
            "tags": tags,
            "tag_ids": post.get("tags") or [],
            "excerpt": html_to_text(excerpt_html),
            "featured_image": featured,
            "content_html": content_html,
            "content_text": html_to_text(content_html),
            "source": SOURCE_NAME,
            "source_category_url": LISTING_URL,
            "source_category_slug": CATEGORY_SLUG,
            "crawled_at": crawled_at,
        }

    def run(self) -> int:
        crawled_at = _now_iso()
        self._load_resume_ids()
        if not self.resume and self.output_path.exists():
            LOGGER.info("Removing existing output (no --resume): %s", self.output_path)
            self.output_path.unlink()

        category_id = self.resolve_category_id()
        LOGGER.info("Using category id=%s slug=%s", category_id, CATEGORY_SLUG)

        sleep_polite(self.delay_min, self.delay_max)
        first_page, headers = self._get_json(
            f"{API_BASE}/posts",
            params={
                "categories": category_id,
                "per_page": self.per_page,
                "page": 1,
                "_embed": "1",
            },
        )
        if not isinstance(first_page, list):
            raise RuntimeError("Unexpected posts payload")

        total = int(headers.get("x-wp-total") or 0)
        total_pages = int(headers.get("x-wp-totalpages") or 1)
        if self.limit_pages is not None:
            total_pages = min(total_pages, max(1, self.limit_pages))

        LOGGER.info(
            "REST total=%s pages=%s per_page=%s limit=%s",
            total,
            total_pages,
            self.per_page,
            self.limit,
        )

        success = 0
        skipped = 0
        failed = 0

        def ingest(posts: list[dict[str, Any]]) -> bool:
            nonlocal success, skipped, failed
            for post in posts:
                post_id = post.get("id")
                if isinstance(post_id, int) and post_id in self._done_ids:
                    skipped += 1
                    continue
                try:
                    record = self.normalize_post(post, crawled_at=crawled_at)
                    if not isinstance(record.get("id"), int):
                        raise RuntimeError("missing id")
                    self._append_jsonl(record)
                    self._done_ids.add(int(record["id"]))
                    success += 1
                    LOGGER.info(
                        "  + id=%s title=%s",
                        record["id"],
                        (record.get("title") or "")[:80],
                    )
                except Exception as exc:
                    failed += 1
                    LOGGER.warning("  ! failed id=%s: %s", post_id, exc)
                if self.limit is not None and success >= self.limit:
                    return False
            return True

        if not ingest(first_page):
            LOGGER.info(
                "Done early. success=%s skipped=%s failed=%s output=%s",
                success,
                skipped,
                failed,
                self.output_path,
            )
            return 0 if success or skipped else 1

        for page in range(2, total_pages + 1):
            sleep_polite(self.delay_min, self.delay_max)
            posts, _ = self._get_json(
                f"{API_BASE}/posts",
                params={
                    "categories": category_id,
                    "per_page": self.per_page,
                    "page": page,
                    "_embed": "1",
                },
            )
            if not isinstance(posts, list) or not posts:
                LOGGER.warning("Empty page %s — stopping", page)
                break
            LOGGER.info("Fetched page=%s/%s count=%s", page, total_pages, len(posts))
            if not ingest(posts):
                break

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
        description="Crawl khodro45 review-compare magazine articles to JSONL.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None, help="Max articles to write")
    parser.add_argument("--limit-pages", type=int, default=None, help="Max REST pages")
    parser.add_argument("--per-page", type=int, default=PER_PAGE)
    parser.add_argument(
        "--category-id",
        type=int,
        default=None,
        help="Override category id (default resolve review-compare)",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN_S)
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX_S)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)
    crawler = Khodro45ArticlesCrawler(
        output_path=args.output,
        resume=args.resume,
        limit=args.limit,
        limit_pages=args.limit_pages,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        per_page=args.per_page,
        category_id=args.category_id,
    )
    try:
        return crawler.run()
    except Exception as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
