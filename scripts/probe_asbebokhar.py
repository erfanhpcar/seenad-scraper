#!/usr/bin/env python3
"""
Probe: asbe-bokhar.com car specs bank (/cars/).

Maps brand archives → model pages → specs (table + #single-spec icons).
WordPress REST is auth-gated (401); HTML crawl is required.

Usage:
  python scripts/probe_asbebokhar.py
  python scripts/probe_asbebokhar.py --limit-brands 2 --limit-cars 2 -v
  docker compose exec -T backend python3 scripts/probe_asbebokhar.py

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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

BASE_URL = "https://asbe-bokhar.com"
LISTING_URL = f"{BASE_URL}/cars/"
OUTPUT_PATH = Path("data/asbebokhar_probe_sample.json")
RAW_LISTING_PATH = Path("data/asbebokhar_listing_probe_raw.html")
REQUEST_TIMEOUT_S = 60.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
BRAND_RE = re.compile(r"^https://asbe-bokhar\.com/cars/([^/]+)/?$")
CAR_RE = re.compile(r"^https://asbe-bokhar\.com/cars/([^/]+)/([^/]+)/?$")


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


def fetch(session: requests.Session, url: str) -> tuple[int, str]:
    response = session.get(url, timeout=REQUEST_TIMEOUT_S)
    response.encoding = response.encoding or "utf-8"
    return response.status_code, response.text


def extract_brands(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, anchor["href"]).split("?")[0]
        match = BRAND_RE.match(href.rstrip("/") + "/") or BRAND_RE.match(href)
        if not match:
            # normalize
            match = BRAND_RE.match(href if href.endswith("/") else href + "/")
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
        if not title:
            title = clean_text(anchor.get_text(" ", strip=True))
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


def extract_icon_specs(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for item in soup.select("#single-spec .single-spec, .single-spec"):
        img = item.find("img")
        label = None
        if img is not None:
            label = clean_text(img.get("title") or img.get("alt"))
        value_el = item.find("span")
        value = clean_text(value_el.get_text(" ", strip=True) if value_el else None)
        if not value:
            chunks = [clean_text(x) for x in item.stripped_strings]
            chunks = [c for c in chunks if c]
            value = chunks[-1] if chunks else None
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


def probe_detail(session: requests.Session, url: str) -> dict[str, Any]:
    status, html = fetch(session, url)
    soup = BeautifulSoup(html, "html.parser")
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    brand_slug = parts[1] if len(parts) >= 3 else None
    model_slug = parts[2] if len(parts) >= 3 else None

    article_ld = None
    for obj in parse_ld_objects(soup):
        if isinstance(obj, dict) and "@graph" in obj:
            for node in obj["@graph"]:
                if isinstance(node, dict) and node.get("@type") == "Article":
                    article_ld = node
                    break
        elif isinstance(obj, dict) and obj.get("@type") == "Article":
            article_ld = obj
        if article_ld:
            break

    icon_specs = extract_icon_specs(soup)
    table_specs = extract_table_specs(soup)
    return {
        "status": status,
        "url": url,
        "html_bytes": len(html),
        "brand_slug": brand_slug,
        "model_slug": model_slug,
        "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
        "headline": clean_text(article_ld.get("headline")) if article_ld else None,
        "date_published": article_ld.get("datePublished") if article_ld else None,
        "date_modified": article_ld.get("dateModified") if article_ld else None,
        "icon_specs": icon_specs,
        "table_specs": table_specs,
        "specs_merged": {**icon_specs, **table_specs},
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe asbe-bokhar.com /cars/ specs bank.")
    parser.add_argument("--limit-brands", type=int, default=2)
    parser.add_argument("--limit-cars", type=int, default=2)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    try:
        _print_section("1) Brand listing /cars/")
        status, html = fetch(session, LISTING_URL)
        print(f"[+] GET {LISTING_URL} status={status} bytes={len(html):,}")
        RAW_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_LISTING_PATH.write_text(html, encoding="utf-8")
        brands = extract_brands(html)
        print(f"[=] brands={len(brands)}")
        for brand in brands[:8]:
            print(f"    - {brand['name']} ({brand['slug']})")

        _print_section("2) Brand pages → cars")
        brand_samples: list[dict[str, Any]] = []
        car_candidates: list[dict[str, Any]] = []
        for brand in brands[: max(1, args.limit_brands)]:
            b_status, b_html = fetch(session, brand["url"])
            cars = extract_brand_cars(b_html, brand_slug=brand["slug"])
            # follow one next page if present
            soup = BeautifulSoup(b_html, "html.parser")
            nxt = (soup.find("link", rel="next") or {}).get("href")
            pages = 1
            while nxt and pages < 3:
                pages += 1
                n_status, n_html = fetch(session, nxt)
                more = extract_brand_cars(n_html, brand_slug=brand["slug"])
                # dedupe
                existing = {c["model_slug"] for c in cars}
                for car in more:
                    if car["model_slug"] not in existing:
                        cars.append(car)
                        existing.add(car["model_slug"])
                nxt = (BeautifulSoup(n_html, "html.parser").find("link", rel="next") or {}).get(
                    "href"
                )
            print(f"[+] {brand['name']}: status={b_status} cars={len(cars)} pages={pages}")
            brand_samples.append({**brand, "status": b_status, "car_count": len(cars), "cars_sample": cars[:5]})
            car_candidates.extend(cars)

        _print_section("3) Detail samples")
        details: list[dict[str, Any]] = []
        for car in car_candidates[: max(1, args.limit_cars)]:
            detail = probe_detail(session, car["url"])
            details.append(detail)
            print(
                f"[+] {detail.get('model_slug')} title={detail.get('headline') or detail.get('title')!r} "
                f"specs={len(detail.get('specs_merged') or {})}"
            )
            print(f"    keys={list((detail.get('specs_merged') or {}))[:10]}")

    except Timeout as exc:
        print(f"[!] timeout: {exc}", file=sys.stderr)
        return 2
    except RequestException as exc:
        print(f"[!] request failed: {exc}", file=sys.stderr)
        return 2

    recommendation = {
        "preferred_strategy": "html_brands_then_model_pages",
        "reason": (
            "WP REST returns 401 unauthenticated. Crawl brand archives under /cars/{brand}/ "
            "then model pages /cars/{brand}/{model}/. Specs from .single-spec icons + tables."
        ),
        "brand_count": len(brands),
        "rest_available": False,
        "output_suggestion": "data/raw_asbebokhar_cars.jsonl",
    }

    report = {
        "probed_at": _now_iso(),
        "source": "asbe-bokhar",
        "listing": {
            "url": LISTING_URL,
            "status": status,
            "brand_count": len(brands),
            "brands_sample": brands[:20],
            "raw_html_path": str(RAW_LISTING_PATH),
        },
        "brand_pages": brand_samples,
        "details": details,
        "recommendation": recommendation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] wrote {args.output.resolve()}")
    _print_section("4) Recommendation")
    print(json.dumps(recommendation, ensure_ascii=False, indent=2))
    return 0 if details else 1


if __name__ == "__main__":
    raise SystemExit(main())
