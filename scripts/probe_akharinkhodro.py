#!/usr/bin/env python3
"""
Probe: akharinkhodro.ir car specs articles (/category/cars/).

Maps listing pagination, WordPress REST coverage, and article field shape
before writing a production crawler.

Findings (encoded as checks this script re-validates):
  - Parent category id=72 (slug=cars) has REST count=0; posts live on child brand cats.
  - HTML archive only shows ~4 posts/page (~34 pages) — incomplete vs REST.
  - REST OR-query of all child category ids returns ~400 posts (preferred path).
  - Article body includes HTML tables for specs + safety/comfort lists.

Usage:
  python scripts/probe_akharinkhodro.py
  python scripts/probe_akharinkhodro.py --limit-posts 3 -v
  docker compose exec -T backend python3 scripts/probe_akharinkhodro.py

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
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

BASE_URL = "https://akharinkhodro.ir"
LISTING_URL = f"{BASE_URL}/category/cars/"
CARS_CATEGORY_ID = 72
OUTPUT_PATH = Path("data/akharinkhodro_probe_sample.json")
RAW_LISTING_PATH = Path("data/akharinkhodro_listing_probe_raw.html")
REQUEST_TIMEOUT_S = 45.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "access denied",
)

CAR_PERMALINK_RE = re.compile(
    r"^https://akharinkhodro\.ir/cars/[^/]+/[^/]+/?$",
    re.IGNORECASE,
)
WS_RE = re.compile(r"\s+")


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = WS_RE.sub(" ", value).strip()
    return text or None


def _html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return _clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def detect_anti_bot(status: int, html: str) -> dict[str, Any]:
    lowered = (html or "").lower()
    hits = [m for m in ANTI_BOT_MARKERS if m in lowered]
    return {
        "status": status,
        "likely_blocked": status in (403, 429, 503) or bool(hits),
        "markers": hits,
    }


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
    header_map = {k.lower(): v for k, v in response.headers.items()}
    return response.status_code, response.text, header_map


def extract_listing_car_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, anchor["href"]).split("?")[0].rstrip("/") + "/"
        if not CAR_PERMALINK_RE.match(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def extract_listing_cards(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []
    for img in soup.select("img.specPostImg"):
        wrap = img
        for _ in range(8):
            if wrap.parent is None:
                break
            wrap = wrap.parent
            classes = " ".join(wrap.get("class") or [])
            if wrap.name == "a" or "spec" in classes.lower():
                # keep climbing a bit to get title siblings
                pass
        link_el = img.find_parent("a")
        # title/details often sit near the image in listing markup
        title_el = None
        details_el = None
        node = img
        for _ in range(10):
            if node is None:
                break
            if title_el is None:
                title_el = node.select_one(".specPostTitle") if hasattr(node, "select_one") else None
            if details_el is None:
                details_el = (
                    node.select_one(".specPostDetails") if hasattr(node, "select_one") else None
                )
            if title_el and details_el:
                break
            node = node.parent
        href = None
        if link_el and link_el.get("href"):
            href = urljoin(BASE_URL, link_el["href"])
        cards.append(
            {
                "url": href,
                "title": _clean_text(title_el.get_text(" ", strip=True)) if title_el else None,
                "details_teaser": (
                    _clean_text(details_el.get_text(" | ", strip=True)) if details_el else None
                ),
                "image_src": img.get("src") or img.get("data-src") or img.get("data-lazy-src"),
                "image_alt": img.get("alt"),
            }
        )
    return cards


def parse_spec_tables(content_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(content_html or "", "html.parser")
    tables_out: list[dict[str, Any]] = []
    for index, table in enumerate(soup.find_all("table")):
        rows: list[dict[str, str]] = []
        kv: dict[str, str] = {}
        for tr in table.find_all("tr"):
            cells = [
                _clean_text(c.get_text(" ", strip=True)) or ""
                for c in tr.find_all(["th", "td"])
            ]
            if not any(cells):
                continue
            rows.append({"cells": cells})
            if len(cells) >= 2 and cells[0]:
                kv[cells[0]] = cells[1]
        tables_out.append(
            {
                "index": index,
                "row_count": len(rows),
                "key_values": kv,
                "rows_sample": rows[:12],
            }
        )
    return tables_out


def probe_listing(session: requests.Session) -> dict[str, Any]:
    _print_section("1) Listing HTML")
    status, html, headers = fetch(session, LISTING_URL, accept="text/html")
    anti = detect_anti_bot(status, html)
    print(f"[+] GET {LISTING_URL}")
    print(f"[=] status={status} bytes={len(html):,} blocked={anti['likely_blocked']}")
    RAW_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_LISTING_PATH.write_text(html, encoding="utf-8")
    print(f"[=] wrote {RAW_LISTING_PATH}")

    soup = BeautifulSoup(html, "html.parser")
    canonical = (soup.find("link", rel="canonical") or {}).get("href")
    next_link = (soup.find("link", rel="next") or {}).get("href")
    car_links = extract_listing_car_links(html)
    cards = extract_listing_cards(html)

    # estimate last page via binary search on /page/N/
    lo, hi = 1, 80
    last_page = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        page_url = f"{BASE_URL}/category/cars/page/{mid}/"
        page_status, page_html, _ = fetch(session, page_url, accept="text/html")
        ok = page_status == 200 and bool(extract_listing_car_links(page_html))
        if ok:
            last_page = mid
            lo = mid + 1
        else:
            hi = mid - 1

    print(f"[=] canonical={canonical}")
    print(f"[=] rel=next={next_link}")
    print(f"[=] car permalinks on page 1: {len(car_links)}")
    print(f"[=] specPost cards on page 1: {len(cards)}")
    print(f"[=] estimated last HTML listing page with cars: {last_page}")
    print(f"[=] HTML archive coverage estimate: ~{last_page * max(len(car_links), 1)} posts")

    return {
        "url": LISTING_URL,
        "status": status,
        "anti_bot": anti,
        "canonical": canonical,
        "rel_next": next_link,
        "content_type": headers.get("content-type"),
        "car_permalinks_page1": car_links,
        "spec_cards_page1": cards,
        "estimated_last_listing_page": last_page,
        "estimated_html_post_count": last_page * len(car_links),
        "raw_html_path": str(RAW_LISTING_PATH),
    }


def probe_rest_taxonomy(session: requests.Session) -> dict[str, Any]:
    _print_section("2) WordPress REST taxonomy")
    status, body, headers = fetch(
        session,
        f"{BASE_URL}/wp-json/wp/v2/categories/{CARS_CATEGORY_ID}",
        accept="application/json",
    )
    parent = json.loads(body) if status == 200 else {"error": body[:300]}
    print(
        f"[=] parent category {CARS_CATEGORY_ID}: "
        f"name={parent.get('name')!r} slug={parent.get('slug')!r} count={parent.get('count')}"
    )

    status, body, headers = fetch(
        session,
        f"{BASE_URL}/wp-json/wp/v2/categories",
        accept="application/json",
        params={"parent": CARS_CATEGORY_ID, "per_page": 100},
    )
    children = json.loads(body) if status == 200 else []
    child_total_header = headers.get("x-wp-total")
    sum_counts = sum(int(c.get("count") or 0) for c in children)
    print(
        f"[=] child brands: returned={len(children)} "
        f"x-wp-total={child_total_header} sum(count)={sum_counts}"
    )
    for child in children[:8]:
        print(
            f"    - id={child.get('id')} count={child.get('count')} "
            f"slug={child.get('slug')} name={child.get('name')}"
        )
    if len(children) > 8:
        print(f"    ... +{len(children) - 8} more")

    return {
        "parent_category": {
            "id": CARS_CATEGORY_ID,
            "name": parent.get("name"),
            "slug": parent.get("slug"),
            "count": parent.get("count"),
            "link": parent.get("link"),
        },
        "child_brand_count": len(children),
        "child_count_sum": sum_counts,
        "child_brands_sample": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "slug": c.get("slug"),
                "count": c.get("count"),
                "link": c.get("link"),
            }
            for c in children[:20]
        ],
        "all_child_ids": [c.get("id") for c in children if c.get("id") is not None],
    }


def probe_rest_posts(
    session: requests.Session,
    child_ids: list[int],
    *,
    limit_posts: int,
) -> dict[str, Any]:
    _print_section("3) WordPress REST posts (OR child categories)")
    if not child_ids:
        print("[!] no child category ids — skip")
        return {"error": "no_child_ids"}

    # First request: authoritative totals with per_page=100
    count_params = {
        "categories": ",".join(str(i) for i in child_ids),
        "per_page": 100,
        "page": 1,
    }
    status, body, headers = fetch(
        session,
        f"{BASE_URL}/wp-json/wp/v2/posts",
        accept="application/json",
        params=count_params,
    )
    total = int(headers.get("x-wp-total") or 0)
    pages_header = int(headers.get("x-wp-totalpages") or 0)
    count_page_len = len(json.loads(body)) if status == 200 else 0
    pages_at_100 = (total + 99) // 100 if total else pages_header
    print(
        f"[=] status={status} x-wp-total={total} "
        f"page1_returned={count_page_len} pages_at_100≈{pages_at_100} "
        f"(header_pages={pages_header})"
    )
    if status != 200:
        print(f"[!] REST posts failed: {body[:400]}")
        return {"status": status, "error": body[:400]}

    # Second request: embed samples only for the first N posts
    sample_params = {
        "categories": ",".join(str(i) for i in child_ids),
        "per_page": min(max(limit_posts, 1), 100),
        "page": 1,
        "_embed": "1",
    }
    status, body, _headers_sample = fetch(
        session,
        f"{BASE_URL}/wp-json/wp/v2/posts",
        accept="application/json",
        params=sample_params,
    )
    if status != 200:
        print(f"[!] REST sample fetch failed: {body[:400]}")
        return {"status": status, "error": body[:400]}

    posts = json.loads(body)
    samples: list[dict[str, Any]] = []
    for post in posts[:limit_posts]:
        title = _html_to_text((post.get("title") or {}).get("rendered"))
        excerpt = _html_to_text((post.get("excerpt") or {}).get("rendered"))
        content_html = (post.get("content") or {}).get("rendered") or ""
        tables = parse_spec_tables(content_html)
        featured = None
        embedded = post.get("_embedded") or {}
        media_list = embedded.get("wp:featuredmedia") or []
        if media_list and isinstance(media_list[0], dict):
            featured = media_list[0].get("source_url")
        term_names: list[str] = []
        for group in embedded.get("wp:term") or []:
            for term in group:
                name = term.get("name")
                if name:
                    term_names.append(name)

        sample = {
            "id": post.get("id"),
            "slug": post.get("slug"),
            "url": post.get("link"),
            "title": title,
            "date": post.get("date"),
            "modified": post.get("modified"),
            "categories": post.get("categories"),
            "tags": post.get("tags"),
            "term_names": term_names,
            "excerpt": excerpt,
            "featured_image": featured,
            "content_html_length": len(content_html),
            "content_text_preview": (_html_to_text(content_html) or "")[:400],
            "spec_tables": tables,
            "spec_key_values_merged": {
                k: v for table in tables for k, v in (table.get("key_values") or {}).items()
            },
        }
        samples.append(sample)
        print(f"[+] post id={sample['id']} title={sample['title']!r}")
        print(f"    url={sample['url']}")
        print(
            f"    tables={len(tables)} "
            f"kv_keys={list(sample['spec_key_values_merged'])[:8]}"
        )

    return {
        "status": status,
        "x_wp_total": total,
        "x_wp_totalpages": pages_at_100,
        "x_wp_totalpages_header": pages_header,
        "count_page_returned": count_page_len,
        "returned": len(posts),
        "samples": samples,
    }


def probe_rss(session: requests.Session) -> dict[str, Any]:
    _print_section("4) Category RSS (coverage check)")
    feed_url = f"{BASE_URL}/category/cars/feed/"
    status, body, headers = fetch(session, feed_url, accept="application/rss+xml,*/*")
    item_count = body.count("<item>")
    print(f"[=] status={status} bytes={len(body):,} <item> count={item_count}")
    return {
        "url": feed_url,
        "status": status,
        "content_type": headers.get("content-type"),
        "item_count": item_count,
        "note": "RSS is incomplete vs full category; use REST for production crawl.",
    }


def build_recommendation(listing: dict[str, Any], taxonomy: dict[str, Any], posts: dict[str, Any]) -> dict[str, Any]:
    return {
        "preferred_strategy": "wordpress_rest_child_categories",
        "reason": (
            "Parent category REST count is 0; OR-query of child brand category ids "
            "returns the full car-article set with content HTML and spec tables. "
            "HTML /category/cars/ pagination only exposes ~4 posts/page and under-counts."
        ),
        "rest_total_posts": posts.get("x_wp_total"),
        "html_estimated_posts": listing.get("estimated_html_post_count"),
        "child_brands": taxonomy.get("child_brand_count"),
        "output_suggestion": "data/raw_akharinkhodro_cars.json",
        "record_fields": [
            "id",
            "url",
            "slug",
            "title",
            "date",
            "modified",
            "brand",
            "categories",
            "excerpt",
            "featured_image",
            "content_html",
            "content_text",
            "specs",
            "crawled_at",
            "source",
        ],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe akharinkhodro.ir /category/cars/ for article crawl planning.",
    )
    parser.add_argument(
        "--limit-posts",
        type=int,
        default=3,
        help="Number of REST post samples to fetch/parse (default: 3)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"Probe sample JSON path (default: {OUTPUT_PATH})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    session = requests.Session()

    try:
        listing = probe_listing(session)
        taxonomy = probe_rest_taxonomy(session)
        posts = probe_rest_posts(
            session,
            [int(i) for i in taxonomy.get("all_child_ids") or []],
            limit_posts=args.limit_posts,
        )
        rss = probe_rss(session)
    except Timeout as exc:
        print(f"[!] timeout: {exc}", file=sys.stderr)
        return 2
    except RequestException as exc:
        print(f"[!] request failed: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"[!] JSON decode failed: {exc}", file=sys.stderr)
        return 2

    recommendation = build_recommendation(listing, taxonomy, posts)

    _print_section("5) Recommendation")
    print(json.dumps(recommendation, ensure_ascii=False, indent=2))

    report = {
        "probed_at": _now_iso(),
        "source": "akharinkhodro",
        "listing": listing,
        "taxonomy": {
            **{k: v for k, v in taxonomy.items() if k != "all_child_ids"},
            "all_child_ids_count": len(taxonomy.get("all_child_ids") or []),
        },
        "rest_posts": posts,
        "rss": rss,
        "recommendation": recommendation,
    }

    # keep sample smaller on disk: drop full child id list already summarized
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[+] wrote probe sample → {args.output.resolve()}")

    if not posts.get("samples"):
        print("[!] no post samples extracted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
