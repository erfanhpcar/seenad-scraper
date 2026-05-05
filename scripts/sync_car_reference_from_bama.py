from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.models import CarEntity

LOGGER = logging.getLogger("bama-car-reference-sync")
BASE_URL = "https://bama.ir/car"
BASE_DOMAIN = "https://bama.ir"
SITEMAP_CAR_URL = "https://bama.ir/sitemap/car"
MAX_LISTING_URLS = 40
MAX_ADS = 700
DEFAULT_PRIORITY_BRANDS = ["jac", "peugeot", "mvm", "chery"]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; rv:124.0) Gecko/20100101 Firefox/124.0",
]
REFERERS = [
    "https://www.google.com/",
    "https://www.google.com/search?q=bama+car",
    "https://bama.ir/",
]


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _ip_safe_delay(min_seconds: float = 3.0, max_seconds: float = 5.0) -> None:
    # Security requirement: always keep a 3-5 second delay between requests.
    time.sleep(random.uniform(min_seconds, max_seconds))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _slug_to_text(slug: str) -> str:
    return slug.replace("-", " ").strip()


def _random_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": random.choice(REFERERS),
        "Origin": BASE_DOMAIN,
        "Connection": "keep-alive",
    }


def _request_with_retry(
    session: requests.Session,
    url: str,
    max_attempts: int = 3,
) -> str | None:
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, headers=_random_headers(), timeout=45)
            response.raise_for_status()
            text = response.text
            _ip_safe_delay()
            return text
        except Exception as exc:
            if attempt >= max_attempts:
                LOGGER.warning("Request failed after %s attempts: %s | %s", max_attempts, url, exc)
                return None
            backoff_seconds = (2 ** attempt) + random.uniform(1.0, 2.0)
            LOGGER.warning("Request failed (attempt %s/%s): %s | backoff %.1fs", attempt, max_attempts, url, backoff_seconds)
            _ip_safe_delay()
            time.sleep(backoff_seconds)
    return None


def _extract_listing_urls_from_sitemap(xml_text: str) -> list[str]:
    locs = re.findall(r"<loc>(https://bama\.ir/[^<]+)</loc>", xml_text)
    listing_urls: list[str] = []
    for url in locs:
        parsed = urlparse(url)
        if not parsed.path.startswith("/car"):
            continue
        if "mileage=" in url:
            listing_urls.append(url)
    if BASE_URL not in listing_urls:
        listing_urls.insert(0, BASE_URL)
    return listing_urls[:MAX_LISTING_URLS]


def _extract_json_ld_payloads(html: str) -> list[Any]:
    matches = re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    payloads: list[Any] = []
    for raw in matches:
        cleaned = raw.strip()
        if not cleaned:
            continue
        try:
            payloads.append(json.loads(cleaned))
        except json.JSONDecodeError:
            continue
    return payloads


def _collect_itemlist_urls(payload: Any) -> list[str]:
    urls: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            schema_type = str(value.get("@type", "")).lower()
            if schema_type == "itemlist":
                for item in value.get("itemListElement", []):
                    if isinstance(item, dict):
                        raw_url = item.get("url") or (item.get("item") or {}).get("url")
                        if isinstance(raw_url, str) and raw_url:
                            urls.add(urljoin(BASE_DOMAIN, raw_url))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sorted(urls)


def _product_to_reference_row(payload: Any, source_url: str) -> tuple[str, str, str, str] | None:
    if not isinstance(payload, dict):
        return None
    schema_type = str(payload.get("@type", "")).lower()
    if schema_type != "product":
        return None

    brand_field = payload.get("brand")
    if isinstance(brand_field, dict):
        brand = _normalize_text(str(brand_field.get("name") or ""))
    else:
        brand = _normalize_text(str(brand_field or ""))

    model = _normalize_text(str(payload.get("model") or payload.get("name") or ""))
    variant = _normalize_text(
        str(
            payload.get("variant")
            or payload.get("vehicleConfiguration")
            or payload.get("trim")
            or payload.get("name")
            or ""
        )
    )
    if not brand or not model:
        return None
    if not variant:
        variant = model
    return brand, model, variant, source_url


def _extract_product_rows_from_ad_html(html: str, ad_url: str) -> set[tuple[str, str, str, str]]:
    rows: set[tuple[str, str, str, str]] = set()
    payloads = _extract_json_ld_payloads(html)
    for payload in payloads:
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            row = _product_to_reference_row(item, source_url=ad_url)
            if row:
                rows.add(row)
    return rows


def _normalize_brand_token(value: str) -> str:
    cleaned = _normalize_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def _ad_priority_key(ad_url: str, priority_tokens: list[str]) -> tuple[int, str]:
    lowered_url = ad_url.lower()
    for idx, token in enumerate(priority_tokens):
        if token and token in lowered_url:
            return idx, lowered_url
    return len(priority_tokens), lowered_url


def _commit_unique_rows_incremental(rows: set[tuple[str, str, str, str]]) -> int:
    if not rows:
        return 0
    db = SessionLocal()
    inserted = 0
    try:
        for brand, model, trim, source_url in sorted(rows):
            exists = db.scalar(
                select(CarEntity.id).where(
                    CarEntity.brand == brand,
                    CarEntity.model == model,
                    CarEntity.trim == trim,
                )
            )
            if exists:
                continue
            db.add(
                CarEntity(
                    brand=brand,
                    model=model,
                    trim=trim,
                    production_year_start=None,
                    production_year_end=None,
                    source_url=source_url,
                )
            )
            inserted += 1
        db.commit()
        return inserted
    finally:
        db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync car reference rows from bama JSON-LD.")
    parser.add_argument("--max-listing-urls", type=int, default=MAX_LISTING_URLS)
    parser.add_argument("--max-ads", type=int, default=MAX_ADS)
    parser.add_argument(
        "--priority-brands",
        type=str,
        default=",".join(DEFAULT_PRIORITY_BRANDS),
        help="Comma-separated brand tokens used for ad URL prioritization.",
    )
    return parser


def main() -> None:
    _configure_logging()
    args = _build_parser().parse_args()
    Base.metadata.create_all(bind=engine, tables=[CarEntity.__table__])
    total_inserted = 0

    session = requests.Session()
    session.verify = False
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    listing_urls: list[str] = [BASE_URL]
    sitemap_xml = _request_with_retry(session, SITEMAP_CAR_URL)
    if sitemap_xml:
        listing_urls = _extract_listing_urls_from_sitemap(sitemap_xml)
    listing_urls = listing_urls[: max(args.max_listing_urls, 1)]
    LOGGER.info("Selected %s listing URLs for JSON-LD crawl.", len(listing_urls))

    ad_urls: set[str] = set()
    for listing_url in listing_urls:
        listing_html = _request_with_retry(session, listing_url)
        if not listing_html:
            continue
        for payload in _extract_json_ld_payloads(listing_html):
            ad_urls.update(_collect_itemlist_urls(payload))
        if len(ad_urls) >= args.max_ads:
            break

    priority_tokens = [
        _normalize_brand_token(item)
        for item in str(args.priority_brands).split(",")
        if _normalize_brand_token(item)
    ]
    if not priority_tokens:
        priority_tokens = DEFAULT_PRIORITY_BRANDS.copy()

    selected_ads = sorted(ad_urls, key=lambda ad_url: _ad_priority_key(ad_url, priority_tokens))[: args.max_ads]
    LOGGER.info("Collected %s ad URLs from listing ItemList schemas.", len(selected_ads))
    LOGGER.info("Priority brand tokens: %s", ", ".join(priority_tokens))

    for idx, ad_url in enumerate(selected_ads, start=1):
        ad_html = _request_with_retry(session, ad_url)
        if not ad_html:
            continue
        rows = _extract_product_rows_from_ad_html(ad_html, ad_url=ad_url)
        inserted = _commit_unique_rows_incremental(rows)
        total_inserted += inserted
        if idx % 25 == 0:
            LOGGER.info("Processed ads: %s/%s | inserted so far: %s", idx, len(selected_ads), total_inserted)

    db = SessionLocal()
    try:
        final_count = db.scalar(select(func.count(CarEntity.id))) or 0
    finally:
        db.close()

    LOGGER.info("Finished sync. inserted=%s final_table_count=%s", total_inserted, final_count)
    print(f"Total extracted combinations: {final_count}")


if __name__ == "__main__":
    main()
