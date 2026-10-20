#!/usr/bin/env python3
"""
Crawler: startmycar.com model guides + owner reviews.

Strategy (from probe_startmycar.py + RAV4/guides mapping):
  - Brand pages /{brand}/pickmodel list models (SSR HTML, no public API).
  - Each model has /{brand}/{model}/guides grouped by category
    (Lubrication / Maintenance / Fuel are the blog-priority groups).
  - Guide bodies are SSR .Article / .js-article HTML (model name swapped into
    a shared template; still stored per model).
  - Reviews are SSR .js-review cards on /{brand}/{model}/reviews (no pagination).
  - Do not fetch /members/profile/* (robots.txt disallow).

Default brands (Salman Kaheh, 22 Shahrivar):
  toyota, ssangyong, volkswagen, mercedes-benz, bmw, mazda, hyundai, kia

Usage:
  python scripts/crawl_startmycar.py --models toyota/rav4 -v
  python scripts/crawl_startmycar.py --models toyota/rav4 --priority-only \\
      --output data/raw_startmycar_guides_reviews_smoke.jsonl -v
  python scripts/crawl_startmycar.py --limit-models 1 --brands toyota -v
  python scripts/crawl_startmycar.py --resume
  docker exec -i seenad_backend python3 scripts/crawl_startmycar.py \\
      --models toyota/rav4 --priority-only -v

Full run:
  nohup docker exec -i seenad_backend python3 scripts/crawl_startmycar.py \\
      --resume -v > data/startmycar_crawl.log 2>&1 &

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
from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("startmycar-crawler")

BASE_URL = "https://www.startmycar.com"
DEFAULT_OUTPUT = Path("data/raw_startmycar_guides_reviews.jsonl")
SOURCE_NAME = "startmycar.com"
REQUEST_TIMEOUT_S = 45.0
DEFAULT_DELAY_MIN_S = 0.45
DEFAULT_DELAY_MAX_S = 1.2
MAX_RETRIES = 4

DEFAULT_BRANDS = (
    "toyota",
    "ssangyong",
    "volkswagen",
    "mercedes-benz",
    "bmw",
    "mazda",
    "hyundai",
    "kia",
)
PRIORITY_CATEGORIES = frozenset({"lubrication", "maintenance", "fuel"})
SKIP_MODEL_SLUGS = frozenset(
    {
        "pickmodel",
        "pickmake",
        "problems",
        "reviews",
        "guides",
        "compare",
        "community",
        "elegirmodelo",
        "escolhermodelo",
        "choosemodel",
    }
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,fa-IR;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Referer": f"{BASE_URL}/pickmake",
}

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
    "arvancloud",
)
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
BRAND_HREF_RE = re.compile(r"^/([a-z0-9-]+)/pickmodel/?$", re.IGNORECASE)
MODEL_HREF_RE = re.compile(r"^/([a-z0-9-]+)/([a-z0-9-]+)/?$", re.IGNORECASE)
GUIDE_PATH_RE = re.compile(
    r"^/([a-z0-9-]+)/([a-z0-9-]+)/guides/([a-z0-9-]+)/?$",
    re.IGNORECASE,
)
BG_IMAGE_RE = re.compile(r"url\((['\"]?)(https?://[^)'\"]+)\1\)", re.IGNORECASE)
FROM_COUNTRY_RE = re.compile(r"\bfrom\s+([A-Za-z][A-Za-z .'-]+)\s*$", re.IGNORECASE)
AVG_REVIEWS_RE = re.compile(
    r"Average from\s+(\d+)\s+reviews:\s*([0-9.]+)",
    re.IGNORECASE,
)

class AntiBotError(RuntimeError):
    pass


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


def detect_anti_bot(html: str) -> list[str]:
    lowered = html.lower()
    return [m for m in ANTI_BOT_MARKERS if m in lowered]


def parse_window_json(html: str, name: str) -> Any:
    needle = f"window.{name}"
    idx = html.find(needle)
    if idx < 0:
        return None
    eq = html.find("=", idx)
    if eq < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(html[eq + 1 :].lstrip())
        return obj
    except json.JSONDecodeError:
        return None


def extract_models(html: str, brand_slug: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        path = urlparse(anchor["href"]).path.rstrip("/")
        match = MODEL_HREF_RE.match(path)
        if not match:
            continue
        if match.group(1).lower() != brand_slug:
            continue
        model_slug = match.group(2).lower()
        if model_slug in SKIP_MODEL_SLUGS:
            continue
        url = urljoin(BASE_URL, path)
        if url in seen:
            continue
        seen.add(url)
        models.append(
            {
                "brand_slug": brand_slug,
                "model_slug": model_slug,
                "name": clean_text(anchor.get_text(" ", strip=True))
                or model_slug.replace("-", " ").title(),
                "url": url,
            }
        )
    return models


def extract_guide_index(html: str, brand_slug: str, model_slug: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    guides: list[dict[str, Any]] = []
    seen: set[str] = set()
    category = None
    container = soup.select_one("div.max-width-700") or soup.select_one(".LayoutContent")
    if container is None:
        return guides
    for child in container.children:
        if not isinstance(child, Tag):
            continue
        classes = child.get("class") or []
        if child.name in {"h1", "h2"} and "TitleGray" in classes:
            category = clean_text(child.get_text(" ", strip=True))
            continue
        if child.name != "a" or "ArticleLink" not in classes:
            continue
        href = child.get("href") or ""
        match = GUIDE_PATH_RE.search(urlparse(href).path)
        if not match:
            continue
        if match.group(1).lower() != brand_slug or match.group(2).lower() != model_slug:
            continue
        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        title_el = child.select_one(".ArticleLink__text")
        img_wrap = child.select_one(".ArticleLink__img")
        teaser = None
        if img_wrap and img_wrap.get("style"):
            bg = BG_IMAGE_RE.search(img_wrap["style"])
            if bg:
                teaser = bg.group(2)
        cat = category or "Other"
        guides.append(
            {
                "url": url,
                "slug": match.group(3).lower(),
                "title": clean_text(title_el.get_text(" ", strip=True) if title_el else child.get_text(" ", strip=True)),
                "category": cat,
                "priority": cat.lower() in PRIORITY_CATEGORIES,
                "teaser_image": teaser,
            }
        )
    return guides


def parse_guide_article(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one(".Article.js-article") or soup.select_one(".js-article") or soup.select_one(".Article")
    if article is None:
        raise RuntimeError("missing .Article body")
    for junk in article.select("script, style, ins.adsbygoogle, .adunit-leader, .adunit"):
        junk.decompose()
    images: list[str] = []
    for img in article.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            images.append(urljoin(BASE_URL, src))
    title_el = article.find(["h1", "h2"])
    title = clean_text(title_el.get_text(" ", strip=True) if title_el else None)
    if not title and soup.title:
        title = clean_text(soup.title.get_text(strip=True))
    content_html = str(article)
    content_text = clean_text(article.get_text(" ", strip=True))
    excerpt = None
    if content_text:
        excerpt = content_text[:500]
    page_modelo = parse_window_json(html, "pageModelo")
    return {
        "title": title,
        "content_html": content_html,
        "content_text": content_text,
        "excerpt": excerpt,
        "images": images,
        "page_modelo": page_modelo if isinstance(page_modelo, dict) else None,
        "canonical_url": url,
    }


def _star_count(card: Tag) -> int:
    n = 0
    for img in card.find_all("img"):
        src = (img.get("src") or "").lower()
        if "icon_star--gold" in src:
            n += 1
    return n


def parse_reviews_page(html: str, page_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    blob = soup.get_text(" ", strip=True)
    avg_match = AVG_REVIEWS_RE.search(blob)
    summary = {
        "review_count_claimed": int(avg_match.group(1)) if avg_match else None,
        "average_score": float(avg_match.group(2)) if avg_match else None,
    }
    reviews: list[dict[str, Any]] = []
    for card in soup.select(".js-review"):
        review_id = card.get("data-model-obj-id")
        trim_el = card.select_one(".ModelTrim")
        best = None
        worst = None
        for block in card.select(".Text"):
            label = clean_text(block.get_text(" ", strip=True)) or ""
            spans = [clean_text(s.get_text(" ", strip=True)) for s in block.find_all("span")]
            spans = [s for s in spans if s]
            if any(s.lower().startswith("the best") for s in spans):
                best = next((s for s in spans if not s.lower().startswith("the best")), None)
            elif any(s.lower().startswith("the worst") for s in spans):
                worst = next((s for s in spans if not s.lower().startswith("the worst")), None)
            elif label.lower().startswith("the best"):
                best = label.split(":", 1)[-1].strip() or None
            elif label.lower().startswith("the worst"):
                worst = label.split(":", 1)[-1].strip() or None
        votes_el = card.select_one(".js-comment-votes")
        author_el = card.select_one(".AuthorShort")
        author = None
        author_country = None
        posted_at = None
        posted_label = None
        profile_url = None
        if author_el:
            name_link = None
            for link in author_el.find_all("a", href=True):
                href = link.get("href") or ""
                if "/members/profile/" in href:
                    profile_url = urljoin(BASE_URL, href)
                    if not link.select_one(".letters"):
                        name_link = link
            if name_link is None:
                named = [a for a in author_el.find_all("a", href=True) if not a.select_one(".letters")]
                name_link = named[0] if named else None
            if name_link:
                author = clean_text(name_link.get("title") or name_link.get_text(" ", strip=True))
            fecha = author_el.select_one("span.fecha")
            if fecha:
                posted_at = fecha.get("title")
                posted_label = clean_text(fecha.get_text(" ", strip=True))
            country_blob = clean_text(author_el.get_text(" ", strip=True)) or ""
            if posted_label:
                country_blob = country_blob.replace(posted_label, "").strip()
            country_match = FROM_COUNTRY_RE.search(country_blob)
            if country_match:
                author_country = clean_text(country_match.group(1))
        reviews.append(
            {
                "id": review_id,
                "url": page_url,
                "trim": clean_text(trim_el.get_text(" ", strip=True) if trim_el else None),
                "stars": _star_count(card),
                "best": best,
                "worst": worst,
                "votes": int(clean_text(votes_el.get_text(strip=True) or "0") or 0) if votes_el else 0,
                "author": author,
                "author_country": author_country,
                "author_profile_url": profile_url,
                "posted_at": posted_at,
                "posted_label": posted_label,
            }
        )
    return summary, reviews


class StartMyCarCrawler:
    def __init__(
        self,
        *,
        output_path: Path,
        resume: bool,
        brands: list[str],
        models: list[str],
        limit: int | None,
        limit_models: int | None,
        delay_min: float,
        delay_max: float,
        priority_only: bool,
        skip_guides: bool,
        skip_reviews: bool,
    ) -> None:
        self.output_path = output_path
        self.resume = resume
        self.brands = [b.strip().lower() for b in brands if b.strip()]
        self.model_filters = [m.strip().lower().strip("/") for m in models if m.strip()]
        self.limit = limit
        self.limit_models = limit_models
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.priority_only = priority_only
        self.skip_guides = skip_guides
        self.skip_reviews = skip_reviews
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._done_keys: set[str] = set()
        self.success = 0
        self.skipped = 0
        self.failed = 0
        self.crawled_at = _now_iso()

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
                key = row.get("record_key") or row.get("url")
                if isinstance(key, str) and key:
                    self._done_keys.add(key)
        LOGGER.info("Resume: loaded %s existing key(s)", len(self._done_keys))

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _hit_limit(self) -> bool:
        return self.limit is not None and self.success >= self.limit

    def fetch_html(self, url: str) -> tuple[int, str, str]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
                html = response.text or ""
                markers = detect_anti_bot(html)
                if markers:
                    raise AntiBotError(f"anti-bot markers {markers} on {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = min(2.0 * attempt, 12.0)
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
                return response.status_code, html, response.url
            except AntiBotError:
                raise
            except (Timeout, RequestException) as exc:
                last_error = exc
                wait = min(2.0 * attempt, 12.0)
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

    def emit(self, record: dict[str, Any]) -> bool:
        key = record.get("record_key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("record missing record_key")
        if key in self._done_keys:
            self.skipped += 1
            return True
        record.setdefault("source", SOURCE_NAME)
        record.setdefault("crawled_at", self.crawled_at)
        self._append_jsonl(record)
        self._done_keys.add(key)
        self.success += 1
        return not self._hit_limit()

    def iter_models(self) -> list[dict[str, Any]]:
        if self.model_filters:
            out = []
            for item in self.model_filters:
                if "/" not in item:
                    raise RuntimeError(f"--models expects brand/model, got {item!r}")
                brand_slug, model_slug = item.split("/", 1)
                out.append(
                    {
                        "brand_slug": brand_slug,
                        "model_slug": model_slug,
                        "name": model_slug.replace("-", " ").title(),
                        "url": urljoin(BASE_URL, f"/{brand_slug}/{model_slug}"),
                    }
                )
            return out

        collected: list[dict[str, Any]] = []
        for brand_slug in self.brands:
            listing = urljoin(BASE_URL, f"/{brand_slug}/pickmodel")
            sleep_polite(self.delay_min, self.delay_max)
            status, html, final = self.fetch_html(listing)
            if status != 200:
                LOGGER.warning("Brand page HTTP %s %s", status, final)
                continue
            models = extract_models(html, brand_slug)
            if self.limit_models is not None:
                models = models[: max(0, self.limit_models)]
            LOGGER.info("Brand %s models=%s (limit_models=%s)", brand_slug, len(models), self.limit_models)
            collected.extend(models)
        return collected

    def crawl_guides(self, model: dict[str, Any]) -> bool:
        brand_slug = model["brand_slug"]
        model_slug = model["model_slug"]
        listing_url = urljoin(BASE_URL, f"/{brand_slug}/{model_slug}/guides")
        sleep_polite(self.delay_min, self.delay_max)
        status, html, final = self.fetch_html(listing_url)
        if status != 200:
            LOGGER.warning("Guides listing HTTP %s %s", status, final)
            self.failed += 1
            return True
        page_modelo = parse_window_json(html, "pageModelo")
        brand_name = None
        model_name = model.get("name")
        if isinstance(page_modelo, dict):
            brand_name = page_modelo.get("marca")
            model_name = page_modelo.get("modelo") or model_name
        index = extract_guide_index(html, brand_slug, model_slug)
        if self.priority_only:
            index = [g for g in index if g.get("priority")]
        LOGGER.info(
            "  guides %s/%s listing=%s priority_only=%s",
            brand_slug,
            model_slug,
            len(index),
            self.priority_only,
        )
        for item in index:
            if self._hit_limit():
                return False
            record_key = item["url"]
            if record_key in self._done_keys:
                self.skipped += 1
                continue
            sleep_polite(self.delay_min, self.delay_max)
            try:
                g_status, g_html, g_final = self.fetch_html(item["url"])
                if g_status != 200:
                    raise RuntimeError(f"HTTP {g_status}")
                parsed = parse_guide_article(g_html, g_final)
            except Exception as exc:
                self.failed += 1
                LOGGER.warning("  ! guide failed %s: %s", item["url"], exc)
                continue
            record = {
                "record_type": "guide",
                "record_key": record_key,
                "url": g_final,
                "brand": brand_name or brand_slug.replace("-", " ").title(),
                "brand_slug": brand_slug,
                "model": model_name,
                "model_slug": model_slug,
                "category": item.get("category"),
                "priority": bool(item.get("priority")),
                "slug": item.get("slug"),
                "listing_title": item.get("title"),
                "teaser_image": item.get("teaser_image"),
                **parsed,
            }
            LOGGER.info(
                "  + guide [%s] %s",
                record.get("category"),
                (record.get("title") or record.get("slug") or "")[:80],
            )
            if not self.emit(record):
                return False
        return True

    def crawl_reviews(self, model: dict[str, Any]) -> bool:
        brand_slug = model["brand_slug"]
        model_slug = model["model_slug"]
        page_url = urljoin(BASE_URL, f"/{brand_slug}/{model_slug}/reviews")
        sleep_polite(self.delay_min, self.delay_max)
        status, html, final = self.fetch_html(page_url)
        if status != 200:
            LOGGER.warning("Reviews HTTP %s %s", status, final)
            self.failed += 1
            return True
        page_modelo = parse_window_json(html, "pageModelo")
        brand_name = brand_slug.replace("-", " ").title()
        model_name = model.get("name")
        if isinstance(page_modelo, dict):
            brand_name = page_modelo.get("marca") or brand_name
            model_name = page_modelo.get("modelo") or model_name
        summary, reviews = parse_reviews_page(html, final)
        LOGGER.info(
            "  reviews %s/%s cards=%s claimed=%s avg=%s",
            brand_slug,
            model_slug,
            len(reviews),
            summary.get("review_count_claimed"),
            summary.get("average_score"),
        )
        for review in reviews:
            if self._hit_limit():
                return False
            review_id = review.get("id") or ""
            record_key = f"review:{review_id}" if review_id else f"review:{final}:{review.get('author')}:{review.get('posted_at')}"
            record = {
                "record_type": "review",
                "record_key": record_key,
                "brand": brand_name,
                "brand_slug": brand_slug,
                "model": model_name,
                "model_slug": model_slug,
                "average_score": summary.get("average_score"),
                "review_count_claimed": summary.get("review_count_claimed"),
                **review,
            }
            if not self.emit(record):
                return False
        return True

    def run(self) -> int:
        self._load_resume_keys()
        if not self.resume and self.output_path.exists():
            LOGGER.info("Removing existing output (no --resume): %s", self.output_path)
            self.output_path.unlink()

        models = self.iter_models()
        LOGGER.info("Models to crawl: %s", len(models))
        if not models:
            LOGGER.error("No models resolved")
            return 1

        for model in models:
            LOGGER.info("Model %s/%s", model["brand_slug"], model["model_slug"])
            if not self.skip_guides:
                if not self.crawl_guides(model):
                    break
            if self._hit_limit():
                break
            if not self.skip_reviews:
                if not self.crawl_reviews(model):
                    break

        LOGGER.info(
            "Done. success=%s skipped_resume=%s failed=%s output=%s",
            self.success,
            self.skipped,
            self.failed,
            self.output_path.resolve(),
        )
        if self.success == 0 and self.skipped == 0:
            return 1
        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl startmycar.com guides + reviews for selected brands.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--brands",
        default=",".join(DEFAULT_BRANDS),
        help="Comma-separated brand slugs (ignored when --models is set)",
    )
    parser.add_argument(
        "--models",
        action="append",
        default=[],
        help="brand/model path to crawl (repeatable). Example: toyota/rav4",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max JSONL records to write")
    parser.add_argument(
        "--limit-models",
        type=int,
        default=None,
        help="Max models per brand (when discovering from pickmodel)",
    )
    parser.add_argument("--priority-only", action="store_true", help="Only Lubrication/Maintenance/Fuel guides")
    parser.add_argument("--skip-guides", action="store_true")
    parser.add_argument("--skip-reviews", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN_S)
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX_S)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.verbose)
    brands = [part.strip() for part in args.brands.split(",") if part.strip()]
    crawler = StartMyCarCrawler(
        output_path=args.output,
        resume=args.resume,
        brands=brands,
        models=args.models,
        limit=args.limit,
        limit_models=args.limit_models,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        priority_only=args.priority_only,
        skip_guides=args.skip_guides,
        skip_reviews=args.skip_reviews,
    )
    try:
        return crawler.run()
    except AntiBotError as exc:
        LOGGER.error("%s", exc)
        return 2
    except Exception as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
