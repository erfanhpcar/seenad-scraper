#!/usr/bin/env python3
"""
Probe: khodrobank.com car specs catalog (/cars).

Maps brand listing → brand pages → car detail fields (JSON-LD Car + topspecs
+ .kb-tcl-hoder spec rows + .compare_single fuel/price lists).

Findings this script re-validates:
  - /cars lists ~117 brands via a.brand_list_item
  - Brand pages list models via a.carspecs__item → /cars/{brand}/{id}/{name}
  - Detail specs are SSR div rows (#kb-spec-car .kb-tcl-hoder), not HTML tables
  - No usable public /api/* endpoint found; HTML crawl is required

Usage:
  python scripts/probe_khodrobank.py
  python scripts/probe_khodrobank.py --url https://www.khodrobank.com/cars/JMC/449/JMC-S350 --limit-brands 1 --limit-cars 0
  python scripts/probe_khodrobank.py --limit-brands 2 --limit-cars 2 -v
  docker compose exec -T backend python3 scripts/probe_khodrobank.py

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
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

BASE_URL = "https://www.khodrobank.com"
LISTING_URL = f"{BASE_URL}/cars"
OUTPUT_PATH = Path("data/khodrobank_probe_sample.json")
RAW_LISTING_PATH = Path("data/khodrobank_listing_probe_raw.html")
REQUEST_TIMEOUT_S = 60.0

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

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
CAR_URL_RE = re.compile(r"/cars/([^/]+)/(\d+)/([^/]+)/?$", re.IGNORECASE)


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


def parse_car_url(url: str) -> dict[str, str | int | None]:
    path = unquote(urlparse(url).path or "")
    match = CAR_URL_RE.search(path)
    if not match:
        return {"brand_slug": None, "car_id": None, "model_slug": None}
    return {
        "brand_slug": match.group(1),
        "car_id": int(match.group(2)),
        "model_slug": match.group(3),
    }


def extract_brands(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict[str, Any]] = []
    for anchor in soup.select("a.brand_list_item"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        name_el = anchor.select_one(".brand_item_name_fa")
        name = clean_text(name_el.get_text(" ", strip=True) if name_el else anchor.get_text(" ", strip=True))
        img = anchor.select_one("img")
        brands.append(
            {
                "name": name,
                "url": url,
                "slug": unquote(urlparse(url).path.rstrip("/").split("/")[-1]),
                "logo": urljoin(BASE_URL, img["src"]) if img and img.get("src") else None,
            }
        )
    return brands


def extract_brand_cars(html: str, *, brand_slug: str | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a.carspecs__item"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(BASE_URL, href)
        parsed = parse_car_url(url)
        if parsed["car_id"] is None:
            continue
        if brand_slug and parsed["brand_slug"] and parsed["brand_slug"] != brand_slug:
            continue
        if url in seen:
            continue
        seen.add(url)
        title_el = anchor.select_one("h5.carspecs__title")
        year_el = anchor.select_one(".carspecs__year")
        drive_el = anchor.select_one(".carspecs__icon__title")
        cars.append(
            {
                "url": url,
                "title": clean_text(title_el.get_text(" ", strip=True) if title_el else None),
                "year_label": clean_text(year_el.get_text(" ", strip=True) if year_el else None),
                "drive_teaser": clean_text(drive_el.get_text(" ", strip=True) if drive_el else None),
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
    """Fallback HTML <table> (skip comparison tables). Primary specs are kb-tcl divs."""
    specs: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = [
            clean_text(c.get_text(" ", strip=True)) or ""
            for c in rows[0].find_all(["th", "td"])
        ]
        # comparison tables have many columns / "نام ماشین" header
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


def probe_detail(session: requests.Session, url: str) -> dict[str, Any]:
    status, html = fetch(session, url)
    soup = BeautifulSoup(html, "html.parser")
    ld_objects = parse_ld_json_objects(soup)
    car_ld = next(
        (o for o in ld_objects if isinstance(o, dict) and o.get("@type") == "Car"),
        None,
    )
    topspecs = extract_topspecs(soup)
    kb_tcl_specs = extract_kb_tcl_specs(soup)
    table_specs = extract_specs_table(soup)
    fuel_specs, price_block = split_compare_blocks(extract_compare_blocks(soup))
    parsed = parse_car_url(url)
    brand = None
    if car_ld and isinstance(car_ld.get("brand"), dict):
        brand = clean_text(car_ld["brand"].get("name"))
    return {
        "status": status,
        "url": url,
        "html_bytes": len(html),
        **parsed,
        "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
        "brand": brand,
        "car_json_ld": car_ld,
        "topspecs": topspecs,
        "kb_tcl_specs": kb_tcl_specs,
        "specs_table": table_specs,
        "fuel": fuel_specs,
        "price_block": price_block,
        "specs_merged": {**topspecs, **table_specs, **kb_tcl_specs, **fuel_specs},
        "ld_types": [o.get("@type") if isinstance(o, dict) else type(o).__name__ for o in ld_objects],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe khodrobank.com /cars catalog.")
    parser.add_argument("--limit-brands", type=int, default=3)
    parser.add_argument("--limit-cars", type=int, default=2)
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Probe this car detail URL (repeatable). Still fetches brand listing.",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    try:
        _print_section("1) Brand listing /cars")
        status, html = fetch(session, LISTING_URL)
        print(f"[+] GET {LISTING_URL} status={status} bytes={len(html):,}")
        RAW_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_LISTING_PATH.write_text(html, encoding="utf-8")
        brands = extract_brands(html)
        print(f"[=] brands={len(brands)}")
        for brand in brands[:8]:
            print(f"    - {brand['name']} -> {brand['url']}")

        _print_section("2) Brand pages → car links")
        brand_samples: list[dict[str, Any]] = []
        car_candidates: list[dict[str, Any]] = []
        for brand in brands[: max(1, args.limit_brands)]:
            b_status, b_html = fetch(session, brand["url"])
            cars = extract_brand_cars(b_html, brand_slug=brand.get("slug"))
            print(f"[+] {brand['name']}: status={b_status} cars={len(cars)}")
            brand_samples.append({**brand, "status": b_status, "car_count": len(cars), "cars_sample": cars[:5]})
            car_candidates.extend(cars)

        _print_section("3) Car detail samples")
        details: list[dict[str, Any]] = []
        detail_urls: list[str] = list(args.url)
        for car in car_candidates:
            if car.get("url") and car["url"] not in detail_urls:
                detail_urls.append(car["url"])
            if len(detail_urls) >= max(1, args.limit_cars) + len(args.url):
                break
        if args.url and not detail_urls:
            detail_urls = list(args.url)
        for url in detail_urls[: max(1, args.limit_cars + len(args.url))]:
            detail = probe_detail(session, url)
            details.append(detail)
            print(
                f"[+] id={detail.get('car_id')} brand={detail.get('brand')!r} "
                f"title={detail.get('title')!r} specs={len(detail.get('specs_merged') or {})} "
                f"html_mb={detail['html_bytes']/1e6:.2f}"
            )
            print(f"    keys={list((detail.get('specs_merged') or {}))[:12]}")

    except Timeout as exc:
        print(f"[!] timeout: {exc}", file=sys.stderr)
        return 2
    except RequestException as exc:
        print(f"[!] request failed: {exc}", file=sys.stderr)
        return 2

    recommendation = {
        "preferred_strategy": "html_brands_then_kb_tcl_specs",
        "reason": (
            "SSR HTML brand list + brand model cards. Detail specs are div rows "
            "(.kb-tcl-hoder), not HTML tables; plus .compare_single fuel/price lists "
            "and schema.org Car JSON-LD. No public JSON API found."
        ),
        "brand_count": len(brands),
        "output_suggestion": "data/raw_khodrobank_cars.json",
        "record_fields": [
            "car_id",
            "url",
            "brand",
            "brand_slug",
            "model_slug",
            "title",
            "name",
            "model",
            "release_date",
            "image",
            "rating",
            "specs",
            "fuel",
            "price",
            "dealership",
            "description",
            "crawled_at",
            "source",
        ],
    }

    report = {
        "probed_at": _now_iso(),
        "source": "khodrobank",
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
