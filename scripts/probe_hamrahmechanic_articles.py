#!/usr/bin/env python3
"""
Probe: hamrah-mechanic.com magazine articles — category car-reviews (معرفی خودرو).

Maps listing pagination and WordPress REST (/mag/wp-json) article fields
before writing a production crawler.

Findings this script re-validates:
  - Category slug=car-reviews id=18 (~809 posts)
  - REST base: https://www.hamrah-mechanic.com/mag/wp-json/wp/v2
  - HTML archive also works (~26/page, ~41 pages) but REST is preferred

Usage:
  python scripts/probe_hamrahmechanic_articles.py
  python scripts/probe_hamrahmechanic_articles.py --limit-posts 3 -v
  docker compose exec -T backend python3 scripts/probe_hamrahmechanic_articles.py

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

BASE_URL = "https://www.hamrah-mechanic.com"
LISTING_URL = f"{BASE_URL}/mag/category/car-reviews/"
API_BASE = f"{BASE_URL}/mag/wp-json/wp/v2"
CATEGORY_SLUG = "car-reviews"
OUTPUT_PATH = Path("data/hamrahmechanic_articles_probe_sample.json")
RAW_LISTING_PATH = Path("data/hamrahmechanic_listing_probe_raw.html")
REQUEST_TIMEOUT_S = 45.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


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


def fetch(
    session: requests.Session,
    url: str,
    *,
    accept: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, str, dict[str, str]]:
    headers = dict(BROWSER_HEADERS)
    if accept:
        headers["Accept"] = accept
    response = session.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_S)
    response.encoding = response.encoding or "utf-8"
    return response.status_code, response.text, {k.lower(): v for k, v in response.headers.items()}


def extract_listing_articles(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in soup.select("article.elementor-post"):
        link = article.select_one("a[href]")
        if not link or not link.get("href"):
            continue
        url = urljoin(BASE_URL, link["href"]).split("?")[0]
        if url in seen:
            continue
        seen.add(url)
        title_el = article.select_one(".elementor-heading-title, h2, h3")
        date_el = article.select_one(".elementor-post-info__item--type-date")
        items.append(
            {
                "url": url,
                "title": clean_text(title_el.get_text(" ", strip=True) if title_el else None),
                "date_label": clean_text(date_el.get_text(" ", strip=True) if date_el else None),
            }
        )
    return items


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe hamrah-mechanic mag car-reviews articles.",
    )
    parser.add_argument("--limit-posts", type=int, default=3)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    session = requests.Session()

    try:
        _print_section("1) HTML listing")
        listing_status, html, _listing_headers = fetch(session, LISTING_URL, accept="text/html")
        print(f"[+] GET {LISTING_URL} status={listing_status} bytes={len(html):,}")
        RAW_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_LISTING_PATH.write_text(html, encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        rel_next = (soup.find("link", rel="next") or {}).get("href")
        listing_items = extract_listing_articles(html)
        page_nums = [
            int(m.group(1))
            for a in soup.find_all("a", href=True)
            if (m := re.search(r"/page/(\d+)/", a["href"]))
        ]
        print(f"[=] articles on page1={len(listing_items)} rel=next={rel_next}")
        print(f"[=] max pagination link={max(page_nums) if page_nums else None}")

        _print_section("2) REST category + posts")
        status, body, headers = fetch(
            session,
            f"{API_BASE}/categories",
            accept="application/json",
            params={"slug": CATEGORY_SLUG},
        )
        cats = json.loads(body) if status == 200 else []
        if not cats:
            print("[!] category not found", body[:300], file=sys.stderr)
            return 1
        cat = cats[0]
        cat_id = int(cat["id"])
        print(
            f"[=] category id={cat_id} name={cat.get('name')!r} "
            f"slug={cat.get('slug')} count={cat.get('count')}"
        )

        status, body, headers = fetch(
            session,
            f"{API_BASE}/posts",
            accept="application/json",
            params={
                "categories": cat_id,
                "per_page": min(max(args.limit_posts, 1), 100),
                "page": 1,
                "_embed": "1",
            },
        )
        total = int(headers.get("x-wp-total") or 0)
        pages_at_requested = int(headers.get("x-wp-totalpages") or 0)
        pages_at_100 = (total + 99) // 100 if total else 0
        posts = json.loads(body) if status == 200 else []
        print(
            f"[=] REST status={status} total={total} "
            f"pages@100≈{pages_at_100} returned={len(posts)}"
        )

        samples: list[dict[str, Any]] = []
        for post in posts[: args.limit_posts]:
            content_html = (post.get("content") or {}).get("rendered") or ""
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
                for term in group:
                    tax = term.get("taxonomy")
                    name = clean_text(term.get("name"))
                    if not name:
                        continue
                    if tax == "post_tag":
                        tags.append(name)
                    elif tax == "category":
                        categories.append(name)
            sample = {
                "id": post.get("id"),
                "url": post.get("link"),
                "slug": post.get("slug"),
                "title": html_to_text((post.get("title") or {}).get("rendered")),
                "date": post.get("date"),
                "modified": post.get("modified"),
                "author": author,
                "categories": categories,
                "tags": tags,
                "excerpt": html_to_text((post.get("excerpt") or {}).get("rendered")),
                "featured_image": featured,
                "content_html_length": len(content_html),
                "content_text_preview": (html_to_text(content_html) or "")[:400],
            }
            samples.append(sample)
            print(
                f"[+] id={sample['id']} title={sample['title']!r} "
                f"content_len={sample['content_html_length']}"
            )

    except Timeout as exc:
        print(f"[!] timeout: {exc}", file=sys.stderr)
        return 2
    except RequestException as exc:
        print(f"[!] request failed: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"[!] json error: {exc}", file=sys.stderr)
        return 2

    recommendation = {
        "preferred_strategy": "wordpress_rest_category",
        "api_base": API_BASE,
        "category_id": cat_id,
        "category_slug": CATEGORY_SLUG,
        "rest_total_posts": total,
        "reason": (
            "Full article HTML is available via /mag/wp-json/wp/v2/posts?categories=18. "
            "Prefer REST over HTML pagination."
        ),
        "output_suggestion": "data/raw_hamrahmechanic_car_reviews.jsonl",
        "record_fields": [
            "id",
            "url",
            "slug",
            "title",
            "date",
            "modified",
            "author",
            "categories",
            "tags",
            "excerpt",
            "featured_image",
            "content_html",
            "content_text",
            "crawled_at",
            "source",
        ],
    }

    report = {
        "probed_at": _now_iso(),
        "source": "hamrah-mechanic-mag",
        "listing": {
            "url": LISTING_URL,
            "status": listing_status,
            "articles_page1": listing_items,
            "rel_next": rel_next,
            "max_page_link": max(page_nums) if page_nums else None,
            "raw_html_path": str(RAW_LISTING_PATH),
        },
        "category": {
            "id": cat_id,
            "name": cat.get("name"),
            "slug": cat.get("slug"),
            "count": cat.get("count"),
            "link": cat.get("link"),
        },
        "rest_posts": {
            "total": total,
            "pages_at_100": pages_at_100,
            "pages_header_for_request": pages_at_requested,
            "samples": samples,
        },
        "recommendation": recommendation,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] wrote {args.output.resolve()}")
    _print_section("3) Recommendation")
    print(json.dumps(recommendation, ensure_ascii=False, indent=2))
    return 0 if samples else 1


if __name__ == "__main__":
    raise SystemExit(main())
