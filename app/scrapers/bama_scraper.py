from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ScrapedListing


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; rv:124.0) Gecko/20100101 Firefox/124.0",
]


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
BAMA_DOMAIN = "https://bama.ir"


@dataclass
class BamaListing:
    external_id: str
    price: Decimal | None
    year: int | None
    mileage: int | None
    body_condition: str | None
    title: str | None
    url: str
    city: str | None
    raw_data: dict[str, Any]


@dataclass
class BamaScrapeResult:
    processed_count: int
    cards_found: int
    sample_listing: dict[str, Any] | None


def _human_delay(min_seconds: float = 2.0, max_seconds: float = 5.0) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def _to_english_digits(value: str) -> str:
    return value.translate(PERSIAN_DIGITS)


def _extract_int(value: str | None) -> int | None:
    if not value:
        return None
    text = _to_english_digits(value)
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    return int(digits)


def _extract_price(value: str | None) -> Decimal | None:
    extracted = _extract_int(value)
    if extracted is None:
        return None
    return Decimal(extracted)


def _extract_external_id(listing_url: str, fallback: str) -> str:
    parsed = urlparse(listing_url)
    query_data = parse_qs(parsed.query)
    for key in ("id", "ad", "listingId", "uid"):
        if key in query_data and query_data[key]:
            return query_data[key][0]

    last_segment = parsed.path.rstrip("/").split("/")[-1]
    if last_segment:
        return last_segment
    return fallback


def _safe_text(node: Any) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _random_headers(listing_url: str) -> dict[str, str]:
    parsed = urlparse(listing_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/car" if parsed.scheme and parsed.netloc else f"{BAMA_DOMAIN}/car"
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "Connection": "keep-alive",
    }


def _fetch_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    _human_delay(3.0, 7.0)
    response = requests.get(url, headers=_random_headers(url), timeout=timeout_seconds)
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _extract_cards_from_html(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        "article",
        ".bama-ad",
        ".search-listing-card",
        '[data-testid*="listing"]',
        "div[class*='listing']",
    ]
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            return cards
    return []


def _parse_card(card: Any, index: int) -> BamaListing | None:
    anchor = card.select_one("a[href]")
    href = anchor.get("href") if anchor else None
    if not href:
        return None

    url = href if href.startswith("http") else f"https://bama.ir{href}"
    external_id = _extract_external_id(url, fallback=f"bama-{index}")

    title = _safe_text(card.select_one("h2, h3, [class*='title']"))
    price_text = _safe_text(card.select_one("[class*='price'], [data-testid*='price']"))
    mileage_text = _safe_text(card.select_one("[class*='mileage'], [class*='km'], [data-testid*='mileage']"))
    year_text = _safe_text(card.select_one("[class*='year'], [data-testid*='year']"))
    body_text = _safe_text(card.select_one("[class*='body'], [class*='condition'], [data-testid*='body']"))
    city_text = _safe_text(card.select_one("[class*='location'], [class*='city'], [data-testid*='location']"))

    raw_data: dict[str, Any] = {
        "source": "bama",
        "title": title,
        "url": url,
        "price_text": price_text,
        "year_text": year_text,
        "mileage_text": mileage_text,
        "body_condition_text": body_text,
    }

    return BamaListing(
        external_id=external_id,
        price=_extract_price(price_text),
        year=_extract_int(year_text),
        mileage=_extract_int(mileage_text),
        body_condition=body_text,
        title=title,
        url=url,
        city=city_text,
        raw_data=raw_data,
    )


def _upsert_listing(db: Session, listing: BamaListing) -> None:
    record = db.scalar(
        select(ScrapedListing).where(
            ScrapedListing.source == "bama",
            ScrapedListing.external_id == listing.external_id,
        )
    )

    payload = {
        "title": listing.title,
        "year": listing.year,
        "body_condition": listing.body_condition,
        "url": listing.url,
        "raw_city": listing.city,
    }

    if record:
        record.price = listing.price
        record.mileage = listing.mileage
        record.city = listing.city
        record.raw_data = {**record.raw_data, **listing.raw_data, **payload}
        return

    db.add(
        ScrapedListing(
            source="bama",
            external_id=listing.external_id,
            price=listing.price,
            mileage=listing.mileage,
            city=listing.city,
            raw_data={**listing.raw_data, **payload},
        )
    )


def scrape_bama_brand(
    db: Session,
    *,
    brand_slug: str = "jac",
    model_slug: str = "j4",
    min_listings: int = 20,
    headless: bool = True,
) -> BamaScrapeResult:
    logger = logging.getLogger(__name__)
    inserted_or_updated = 0
    cards_found = 0
    sample_listing: dict[str, Any] | None = None
    target_url = f"https://bama.ir/car/{brand_slug}/{model_slug}"

    if headless is not None:
        logger.debug("headless parameter ignored in requests-based scraper")
    logger.info("Opening Bama listing URL via requests: %s", target_url)
    html = _fetch_listing_html(target_url)
    cards = _extract_cards_from_html(html)
    cards_found = len(cards)
    logger.info("Parsed %s cards from listing HTML", cards_found)

    for index, card in enumerate(cards):
        listing = _parse_card(card, index=index)
        if listing is None:
            logger.debug("Card %s skipped because parsing returned None.", index + 1)
            continue
        _upsert_listing(db, listing)
        inserted_or_updated += 1
        if index < 5:
            logger.info(
                "Extracted card %s/%s: price=%s year=%s mileage=%s body=%s",
                index + 1,
                cards_found,
                listing.price,
                listing.year,
                listing.mileage,
                listing.body_condition,
            )
        if sample_listing is None:
            sample_listing = {
                "external_id": listing.external_id,
                "title": listing.title,
                "price": str(listing.price) if listing.price is not None else None,
                "year": listing.year,
                "mileage": listing.mileage,
                "body_condition": listing.body_condition,
                "url": listing.url,
            }

        if inserted_or_updated >= min_listings:
            break

    db.commit()

    return BamaScrapeResult(
        processed_count=inserted_or_updated,
        cards_found=cards_found,
        sample_listing=sample_listing,
    )
