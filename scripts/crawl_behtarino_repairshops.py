#!/usr/bin/env python3
"""
Two-phase crawler for Behtarino.com car repair shops (تعمیرگاه-خودرو).

Phase 1: Paginate /r/{category}/{city}?page=N; extract LocalBusiness rows from
          SearchResultsPage JSON-LD (name, address, geo, rating, hours, images).
Phase 2: Fetch each /p/{slug} detail page for telephone, description, categories,
          social links, and website; append JSONL rows.

Important: Behtarino's country-wide /r/{category} ranking is Tehran-heavy (often
Tehran-only for this category). For all of Iran, use --all-cities so each city
slug under the category page is crawled separately.

Usage (Docker):
  docker exec -i seenad_backend python3 scripts/crawl_behtarino_repairshops.py --all-cities --skip-cities تهران --resume
  docker exec -i seenad_backend python3 scripts/crawl_behtarino_repairshops.py --city اصفهان
  docker exec -i seenad_backend python3 scripts/crawl_behtarino_repairshops.py --limit-pages 2 -v

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests import Response
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("behtarino-repairshops-crawler")

BASE_URL = "https://behtarino.com"
DEFAULT_CATEGORY = "تعمیرگاه-خودرو"
DEFAULT_OUTPUT = Path("data/raw_behtarino_repairshops.jsonl")
DEFAULT_CITIES_DONE = Path("data/behtarino_repairshops_cities_done.txt")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_DELAY_MIN = 1.5
REQUEST_DELAY_MAX = 3.5
CITY_HREF_RE = re.compile(r'href="(/r/[^"]+)"')

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "access denied",
)
MOBILE_PHONE_RE = re.compile(r"\b09\d{9}\b")
LANDLINE_PHONE_RE = re.compile(r"\b0[1-8]\d{9}\b")
GEO_JSON_RE = re.compile(
    r'\{"@type":"GeoCoordinates","latitude":([-\d.]+),"longitude":([-\d.]+)\}'
)
SOCIAL_HOSTS = (
    "instagram.com",
    "t.me",
    "telegram.me",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "ble.ir",
)
BEHTARINO_NOISE_HOSTS = (
    "behtarino.com",
    "instagram.com/behtarino",
    "linkedin.com/company/behtarino",
    "t.me/behtarino",
    "youtube.com/@behtarino",
    "trustseal.enamad.ir",
    "ble.ir/behtarinobot",
)


@dataclass
class ListingBusiness:
    name: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    profile_url: str
    slug: str | None
    position: int | None
    listing_page: int
    street_address: str | None = None
    address_locality: str | None = None
    address_region: str | None = None
    address_country: str | None = None
    images: list[str] = field(default_factory=list)
    rating_value: float | None = None
    review_count: int | None = None
    best_rating: float | None = None
    opening_hours: list[dict[str, Any]] = field(default_factory=list)
    listing_json_ld_item: dict[str, Any] = field(repr=False, default_factory=dict)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def build_listing_base_url(category: str, city: str | None) -> str:
    path = f"/r/{category}"
    if city:
        path = f"{path}/{city}"
    return BASE_URL + quote(path, safe="/:")


def listing_page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}page={page}"


def discover_city_slugs(
    session: requests.Session,
    category: str,
    *,
    timeout: float = 30.0,
) -> list[str]:
    """Parse city links from the category landing page (/r/{category})."""
    listing_url = build_listing_base_url(category, None)
    response = session.get(listing_url, timeout=timeout)
    response.encoding = "utf-8"
    if response.status_code != 200:
        raise RuntimeError(
            f"City discovery failed HTTP {response.status_code} for {listing_url}"
        )
    if _is_blocked_response(response.status_code, response.text):
        raise RuntimeError(f"City discovery blocked on {listing_url}")

    prefix = f"/r/{category}/"
    cities: list[str] = []
    seen: set[str] = set()
    for href in CITY_HREF_RE.findall(response.text):
        path = unquote(href.split("?", 1)[0]).rstrip("/")
        if not path.startswith(prefix):
            continue
        city = path[len(prefix) :].strip().rstrip("\\")
        if not city or "/" in city or city in seen:
            continue
        seen.add(city)
        cities.append(city)

    if not cities:
        raise RuntimeError(f"No city links found on {listing_url}")
    return cities


def _load_done_cities(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        city = line.strip()
        if city:
            done.add(city)
    return done


def _mark_city_done(path: Path, city: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(city + "\n")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _profile_slug(profile_url: str) -> str | None:
    marker = "/p/"
    idx = profile_url.find(marker)
    if idx < 0:
        return None
    slug_part = profile_url[idx + len(marker) :].split("~", 1)[0].split("?", 1)[0]
    return slug_part.strip() or None


def _format_address(
    street: str | None,
    locality: str | None,
    region: str | None,
) -> str | None:
    parts = [part.strip() for part in (street, locality, region) if part and str(part).strip()]
    if not parts:
        return None
    return "، ".join(dict.fromkeys(parts))


def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("98") and len(digits) >= 12:
        digits = "0" + digits[2:]
    return digits


def _dedupe_phones(phones: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for phone in phones:
        normalized = _normalize_phone(phone)
        if len(normalized) < 10 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v and str(v).strip()))


def _is_blocked_response(status: int, html: str) -> bool:
    if status in {403, 429, 503}:
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in ANTI_BOT_MARKERS)


def _is_noise_url(url: str) -> bool:
    lowered = url.lower()
    return any(noise in lowered for noise in BEHTARINO_NOISE_HOSTS)


def _normalize_images(value: Any) -> list[str]:
    images: list[str] = []
    if isinstance(value, str) and value.strip():
        images.append(value.strip())
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                images.append(entry.strip())
            elif isinstance(entry, dict):
                url = entry.get("url") or entry.get("contentUrl")
                if isinstance(url, str) and url.strip():
                    images.append(url.strip())
    elif isinstance(value, dict):
        url = value.get("url") or value.get("contentUrl")
        if isinstance(url, str) and url.strip():
            images.append(url.strip())
    return _dedupe_strings(images)


def _normalize_opening_hours(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    hours: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        hours.append(
            {
                "day_of_week": entry.get("dayOfWeek"),
                "opens": entry.get("opens"),
                "closes": entry.get("closes"),
            }
        )
    return hours


def _parse_aggregate_rating(value: Any) -> tuple[float | None, int | None, float | None]:
    if not isinstance(value, dict):
        return None, None, None
    return (
        _optional_float(value.get("ratingValue")),
        _optional_int(value.get("reviewCount") or value.get("ratingCount")),
        _optional_float(value.get("bestRating")),
    )


def extract_search_results_json_ld(html: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    soup = BeautifulSoup(html, "html.parser")
    businesses: list[dict[str, Any]] = []
    page_payload: dict[str, Any] | None = None

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        if not isinstance(payload, dict) or payload.get("@type") != "SearchResultsPage":
            continue

        page_payload = payload
        main_entity = payload.get("mainEntity")
        if not isinstance(main_entity, dict):
            continue

        for element in main_entity.get("itemListElement", []):
            if not isinstance(element, dict):
                continue
            item = element.get("item")
            if not isinstance(item, dict):
                continue

            item_type = item.get("@type")
            if item_type not in {None, "LocalBusiness", "Organization", "AutoRepair"}:
                continue

            geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
            address = item.get("address") if isinstance(item.get("address"), dict) else {}
            profile_url = item.get("url")
            if not isinstance(profile_url, str) or not profile_url.strip():
                continue

            rating_value, review_count, best_rating = _parse_aggregate_rating(
                item.get("aggregateRating")
            )

            businesses.append(
                {
                    "position": element.get("position"),
                    "name": item.get("name"),
                    "profile_url": profile_url.strip(),
                    "latitude": _optional_float(geo.get("latitude")),
                    "longitude": _optional_float(geo.get("longitude")),
                    "street_address": address.get("streetAddress"),
                    "address_locality": address.get("addressLocality"),
                    "address_region": address.get("addressRegion"),
                    "address_country": address.get("addressCountry"),
                    "telephone": item.get("telephone"),
                    "images": _normalize_images(item.get("image")),
                    "rating_value": rating_value,
                    "review_count": review_count,
                    "best_rating": best_rating,
                    "opening_hours": _normalize_opening_hours(
                        item.get("openingHoursSpecification")
                    ),
                    "json_ld_item": item,
                }
            )

    return businesses, page_payload


def parse_listing_row(row: dict[str, Any], *, listing_page: int) -> ListingBusiness | None:
    profile_url = row.get("profile_url")
    if not isinstance(profile_url, str) or not profile_url.strip():
        return None

    profile_url = profile_url.strip()
    if not profile_url.startswith("http"):
        profile_url = urljoin(BASE_URL, profile_url)

    street = row.get("street_address") if isinstance(row.get("street_address"), str) else None
    locality = row.get("address_locality") if isinstance(row.get("address_locality"), str) else None
    region = row.get("address_region") if isinstance(row.get("address_region"), str) else None
    country = row.get("address_country") if isinstance(row.get("address_country"), str) else None
    json_ld_item = row.get("json_ld_item")
    if not isinstance(json_ld_item, dict):
        json_ld_item = {}

    images = row.get("images") if isinstance(row.get("images"), list) else []
    opening_hours = row.get("opening_hours") if isinstance(row.get("opening_hours"), list) else []

    return ListingBusiness(
        name=row.get("name") if isinstance(row.get("name"), str) else None,
        address=_format_address(street, locality, region),
        latitude=_optional_float(row.get("latitude")),
        longitude=_optional_float(row.get("longitude")),
        profile_url=profile_url,
        slug=_profile_slug(profile_url),
        position=_optional_int(row.get("position")),
        listing_page=listing_page,
        street_address=street,
        address_locality=locality,
        address_region=region,
        address_country=country,
        images=[str(x) for x in images if isinstance(x, str)],
        rating_value=_optional_float(row.get("rating_value")),
        review_count=_optional_int(row.get("review_count")),
        best_rating=_optional_float(row.get("best_rating")),
        opening_hours=[x for x in opening_hours if isinstance(x, dict)],
        listing_json_ld_item=json_ld_item,
    )


def _iter_json_ld_nodes(payload: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        nodes.append(payload)
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for entry in graph:
                if isinstance(entry, dict):
                    nodes.append(entry)
    elif isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                nodes.append(entry)
    return nodes


def _extract_json_ld_blocks(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            blocks.append(payload)
        elif isinstance(payload, list):
            blocks.extend(entry for entry in payload if isinstance(entry, dict))
    return blocks


def extract_detail_enrichment(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = _extract_json_ld_blocks(html)

    phones: list[str] = []
    tel_links = [
        anchor["href"].replace("tel:", "").strip()
        for anchor in soup.select('a[href^="tel:"]')
        if anchor.get("href")
    ]
    phones.extend(tel_links)
    phones.extend(MOBILE_PHONE_RE.findall(html))
    phones.extend(LANDLINE_PHONE_RE.findall(html))

    description: str | None = None
    images: list[str] = []
    opening_hours: list[dict[str, Any]] = []
    rating_value: float | None = None
    review_count: int | None = None
    best_rating: float | None = None
    same_as: list[str] = []
    identifier: str | None = None
    telephone_from_json_ld: list[str] = []

    business_types = {"LocalBusiness", "Organization", "AutoRepair"}

    for block in blocks:
        for node in _iter_json_ld_nodes(block):
            node_type = node.get("@type")
            if node_type not in business_types and node_type is not None:
                # Still accept nodes that carry phone/rating even without a known type.
                if "telephone" not in node and "aggregateRating" not in node:
                    continue

            tel = node.get("telephone")
            if isinstance(tel, str) and tel.strip():
                telephone_from_json_ld.append(tel.strip())
            elif isinstance(tel, list):
                telephone_from_json_ld.extend(
                    str(x).strip() for x in tel if isinstance(x, str) and x.strip()
                )

            if description is None:
                desc = node.get("description")
                if isinstance(desc, str) and desc.strip():
                    description = desc.strip()

            images.extend(_normalize_images(node.get("image")))

            if not opening_hours:
                opening_hours = _normalize_opening_hours(node.get("openingHoursSpecification"))

            rv, rc, br = _parse_aggregate_rating(node.get("aggregateRating"))
            if rating_value is None and rv is not None:
                rating_value, review_count, best_rating = rv, rc, br

            same = node.get("sameAs")
            if isinstance(same, str) and same.strip():
                same_as.append(same.strip())
            elif isinstance(same, list):
                same_as.extend(str(x).strip() for x in same if isinstance(x, str) and x.strip())

            if identifier is None:
                ident = node.get("identifier")
                if isinstance(ident, str) and ident.strip():
                    identifier = ident.strip()
                elif isinstance(ident, (int, float)):
                    identifier = str(ident)

    phones.extend(telephone_from_json_ld)
    phones.extend(re.findall(r'"telephone"\s*:\s*"([^"]+)"', html))
    phones = _dedupe_phones(phones)

    category_links: list[dict[str, str]] = []
    seen_categories: set[str] = set()
    for anchor in soup.select('a[href^="/r/"]'):
        href = str(anchor.get("href") or "").strip()
        text = anchor.get_text(" ", strip=True)
        if not href or not text:
            continue
        key = f"{href}|{text}"
        if key in seen_categories:
            continue
        seen_categories.add(key)
        category_links.append({"href": href, "text": text})

    # Prefer category slug roots (drop city-only noise later in record builder).
    category_slugs: list[str] = []
    for row in category_links:
        parts = [p for p in row["href"].strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] == "r":
            category_slugs.append(parts[1])
    category_slugs = _dedupe_strings(category_slugs)

    social_links: list[str] = []
    external_websites: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href.startswith("http") or _is_noise_url(href):
            continue
        lowered = href.lower()
        if any(host in lowered for host in SOCIAL_HOSTS):
            social_links.append(href)
        elif "api.whatsapp.com" in lowered or "wa.me" in lowered:
            social_links.append(href)
        else:
            external_websites.append(href)

    social_links = _dedupe_strings(social_links + same_as)
    social_links = [url for url in social_links if not _is_noise_url(url)]
    external_websites = [
        url for url in _dedupe_strings(external_websites) if not _is_noise_url(url)
    ]

    if description is None:
        meta = soup.find("meta", attrs={"name": "description"})
        if isinstance(meta, Tag) and meta.get("content"):
            description = str(meta["content"]).strip() or None

    phone_audit = {
        "tel_links": tel_links[:10],
        "json_ld_telephones": telephone_from_json_ld[:10],
        "mobile_pattern_hits": sorted(set(MOBILE_PHONE_RE.findall(html)))[:10],
        "landline_pattern_hits": sorted(set(LANDLINE_PHONE_RE.findall(html)))[:10],
        "geo_in_html": [
            {"latitude": float(lat), "longitude": float(lng)}
            for lat, lng in GEO_JSON_RE.findall(html)[:3]
        ],
    }

    return {
        "phones": phones,
        "description": description,
        "images": _dedupe_strings(images),
        "opening_hours": opening_hours,
        "rating_value": rating_value,
        "review_count": review_count,
        "best_rating": best_rating,
        "categories": category_slugs[:40],
        "category_links": category_links[:40],
        "social_links": social_links[:20],
        "external_websites": external_websites[:10],
        "identifier": identifier,
        "phone_audit": phone_audit,
    }


def _build_record(
    listing: ListingBusiness,
    *,
    category: str,
    city: str | None,
    enrichment: dict[str, Any],
) -> dict[str, Any]:
    phones = enrichment.get("phones") if isinstance(enrichment.get("phones"), list) else []
    detail_images = enrichment.get("images") if isinstance(enrichment.get("images"), list) else []
    images = _dedupe_strings(listing.images + [str(x) for x in detail_images if x])

    opening_hours = listing.opening_hours or enrichment.get("opening_hours") or []
    rating_value = listing.rating_value
    if rating_value is None:
        rating_value = _optional_float(enrichment.get("rating_value"))
    review_count = listing.review_count
    if review_count is None:
        review_count = _optional_int(enrichment.get("review_count"))
    best_rating = listing.best_rating
    if best_rating is None:
        best_rating = _optional_float(enrichment.get("best_rating"))

    return {
        "source": "behtarino.com",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "city_filter": city,
        "name": listing.name,
        "address": listing.address,
        "street_address": listing.street_address,
        "address_locality": listing.address_locality,
        "address_region": listing.address_region,
        "address_country": listing.address_country,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "profile_url": listing.profile_url,
        "slug": listing.slug,
        "phones": phones,
        "description": enrichment.get("description"),
        "images": images,
        "opening_hours": opening_hours,
        "rating_value": rating_value,
        "review_count": review_count,
        "best_rating": best_rating,
        "categories": enrichment.get("categories") or [],
        "social_links": enrichment.get("social_links") or [],
        "external_websites": enrichment.get("external_websites") or [],
        "identifier": enrichment.get("identifier"),
        "listing_page": listing.listing_page,
        "position": listing.position,
        "raw_payload": {
            "listing_json_ld_item": listing.listing_json_ld_item,
            "detail_phone_audit": enrichment.get("phone_audit"),
            "detail_category_links": enrichment.get("category_links"),
        },
    }


class BehtarinoRepairShopCrawler:
    def __init__(
        self,
        *,
        category: str,
        city: str | None,
        output_path: Path,
        limit_pages: int | None = None,
        delay_min: float = REQUEST_DELAY_MIN,
        delay_max: float = REQUEST_DELAY_MAX,
        max_retries: int = 3,
        timeout: float = 30.0,
        resume: bool = False,
    ) -> None:
        self.category = category
        self.city = city
        self.output_path = output_path
        self.limit_pages = limit_pages
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.resume = resume

        self._listing_base_url = build_listing_base_url(category, city)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._processed_urls: set[str] = set()

    def _sleep_politeness(self) -> None:
        if self.delay_max <= 0:
            return
        low = min(self.delay_min, self.delay_max)
        high = max(self.delay_min, self.delay_max)
        time.sleep(random.uniform(low, high))

    def _fetch_html(self, url: str) -> tuple[int, str]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response: Response = self._session.get(url, timeout=self.timeout)
                response.encoding = "utf-8"
                return response.status_code, response.text
            except (RequestException, Timeout) as exc:
                last_error = exc
                LOGGER.warning(
                    "Request failed (%s/%s) url=%s error=%s",
                    attempt,
                    self.max_retries,
                    url,
                    exc,
                )
                if attempt < self.max_retries:
                    time.sleep(min(2.0 * attempt, 6.0))
        assert last_error is not None
        raise last_error

    def _load_existing_profile_urls(self) -> None:
        if not self.resume or not self.output_path.is_file():
            return

        with self.output_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Skipping corrupt JSONL line %s", line_no)
                    continue
                profile_url = row.get("profile_url")
                if isinstance(profile_url, str) and profile_url.strip():
                    self._processed_urls.add(profile_url.strip())

        if self._processed_urls:
            LOGGER.info(
                "Resume: skipping %s profile URL(s) already in %s",
                len(self._processed_urls),
                self.output_path,
            )

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def collect_listings(self) -> list[ListingBusiness]:
        seen_urls: dict[str, ListingBusiness] = {}
        page = 1
        pages_fetched = 0
        empty_streak = 0

        while True:
            if self.limit_pages is not None and pages_fetched >= self.limit_pages:
                LOGGER.info("Stopping listing phase: --limit-pages=%s", self.limit_pages)
                break

            url = listing_page_url(self._listing_base_url, page)
            LOGGER.info("Listing page=%s url=%s", page, url)

            if pages_fetched > 0:
                self._sleep_politeness()

            try:
                status, html = self._fetch_html(url)
            except Exception as exc:
                LOGGER.error(
                    "Listing page=%s failed after %s retries: %s",
                    page,
                    self.max_retries,
                    exc,
                )
                break

            # Behtarino returns HTTP 500 past the last valid page (~166 Iran-wide).
            if status in {404, 500}:
                LOGGER.info(
                    "Listing page=%s returned HTTP %s; pagination complete",
                    page,
                    status,
                )
                break

            if status != 200:
                LOGGER.warning(
                    "Listing page=%s returned HTTP %s; stopping pagination",
                    page,
                    status,
                )
                break

            if _is_blocked_response(status, html):
                LOGGER.error(
                    "Anti-bot or challenge page detected on listing page=%s — aborting",
                    page,
                )
                break

            rows, _page_payload = extract_search_results_json_ld(html)
            if not rows:
                empty_streak += 1
                LOGGER.info(
                    "No LocalBusiness items in JSON-LD on page=%s (empty_streak=%s)",
                    page,
                    empty_streak,
                )
                if empty_streak >= 2:
                    LOGGER.info("Two empty listing pages in a row; pagination complete")
                    break
                pages_fetched += 1
                page += 1
                continue

            empty_streak = 0
            for row in rows:
                listing = parse_listing_row(row, listing_page=page)
                if listing is not None:
                    seen_urls.setdefault(listing.profile_url, listing)

            LOGGER.info(
                "page=%s json_ld_rows=%s unique_total=%s",
                page,
                len(rows),
                len(seen_urls),
            )

            pages_fetched += 1
            page += 1

        return list(seen_urls.values())

    def scrape_details(self, listings: list[ListingBusiness]) -> tuple[int, int, int]:
        success = 0
        skipped_resume = 0
        failed = 0
        detail_attempts = 0

        for index, listing in enumerate(listings, start=1):
            if listing.profile_url in self._processed_urls:
                skipped_resume += 1
                continue

            if detail_attempts > 0:
                self._sleep_politeness()
            detail_attempts += 1

            LOGGER.info(
                "Detail %s/%s slug=%s url=%s",
                index,
                len(listings),
                listing.slug,
                listing.profile_url,
            )

            try:
                status, html = self._fetch_html(listing.profile_url)
            except Exception as exc:
                LOGGER.warning(
                    "Detail fetch failed url=%s error=%s — skipping",
                    listing.profile_url,
                    exc,
                )
                failed += 1
                continue

            if status == 404:
                LOGGER.warning("Detail 404 url=%s — skipping", listing.profile_url)
                failed += 1
                continue

            if status != 200:
                LOGGER.warning(
                    "Detail HTTP %s url=%s — skipping",
                    status,
                    listing.profile_url,
                )
                failed += 1
                continue

            if _is_blocked_response(status, html):
                LOGGER.warning(
                    "Anti-bot page on detail url=%s — skipping",
                    listing.profile_url,
                )
                failed += 1
                continue

            try:
                enrichment = extract_detail_enrichment(html)
                listing_tel = listing.listing_json_ld_item.get("telephone")
                if isinstance(listing_tel, str) and listing_tel.strip():
                    enrichment["phones"] = _dedupe_phones(
                        list(enrichment.get("phones") or []) + [listing_tel.strip()]
                    )

                record = _build_record(
                    listing,
                    category=self.category,
                    city=self.city,
                    enrichment=enrichment,
                )
                self._append_jsonl(record)
                self._processed_urls.add(listing.profile_url)
                success += 1
            except Exception as exc:
                LOGGER.warning(
                    "Detail serialize failed url=%s error=%s — skipping",
                    listing.profile_url,
                    exc,
                )
                failed += 1

        return success, skipped_resume, failed

    def run(self) -> int:
        self._load_existing_profile_urls()
        listings = self.collect_listings()
        LOGGER.info(
            "Collected %s unique repair-shop listing(s) from %s",
            len(listings),
            self._listing_base_url,
        )

        if not listings:
            LOGGER.warning("No listings collected; nothing to scrape")
            return 1

        success, skipped_resume, failed = self.scrape_details(listings)
        LOGGER.info(
            "Done. detail_success=%s detail_skipped_resume=%s detail_failed=%s output=%s",
            success,
            skipped_resume,
            failed,
            self.output_path.resolve(),
        )
        if success > 0 or skipped_resume > 0:
            return 0
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl Behtarino.com car repair shops into JSONL. "
            "Prefer --all-cities for Iran coverage (country-wide listing is Tehran-biased)."
        ),
    )
    parser.add_argument(
        "--category",
        default=DEFAULT_CATEGORY,
        help=f"Category slug under /r/ (default: {DEFAULT_CATEGORY})",
    )
    parser.add_argument(
        "--city",
        default=None,
        help="Single city slug under /r/{category}/{city}",
    )
    parser.add_argument(
        "--all-cities",
        action="store_true",
        help="Discover and crawl every city linked from /r/{category}",
    )
    parser.add_argument(
        "--skip-cities",
        nargs="*",
        default=[],
        metavar="CITY",
        help="City slugs to skip when using --all-cities (e.g. تهران)",
    )
    parser.add_argument(
        "--cities-done-file",
        type=Path,
        default=DEFAULT_CITIES_DONE,
        help=f"Checkpoint of completed cities (default: {DEFAULT_CITIES_DONE})",
    )
    parser.add_argument(
        "--limit-pages",
        type=int,
        default=None,
        metavar="N",
        help="Stop listing phase after N pages per city (testing)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip profile_url values already present in the output JSONL",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="HTTP retries per request on transient failures",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def _resolve_cities(args: argparse.Namespace, session: requests.Session) -> list[str | None]:
    if args.all_cities and args.city:
        raise SystemExit("Use either --city or --all-cities, not both")

    if args.all_cities:
        cities = discover_city_slugs(session, args.category, timeout=args.timeout)
        skip = {c.strip() for c in args.skip_cities if isinstance(c, str) and c.strip()}
        filtered = [c for c in cities if c not in skip]
        LOGGER.info(
            "Discovered %s cities; skipping %s (%s); crawling %s",
            len(cities),
            sorted(skip),
            len(cities) - len(filtered),
            len(filtered),
        )
        return filtered

    if isinstance(args.city, str) and args.city.strip():
        return [args.city.strip()]

    LOGGER.warning(
        "No --city / --all-cities: crawling country-wide /r/%s "
        "(Behtarino often returns Tehran-only for this category)",
        args.category,
    )
    return [None]


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose)

    bootstrap = requests.Session()
    bootstrap.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
        }
    )

    try:
        cities = _resolve_cities(args, bootstrap)
    except Exception as exc:
        LOGGER.error("Failed to resolve cities: %s", exc)
        return 1

    if not cities:
        LOGGER.error("No cities left to crawl after filters")
        return 1

    done_cities = _load_done_cities(args.cities_done_file) if args.all_cities else set()
    if done_cities:
        LOGGER.info(
            "City checkpoint: %s already done in %s",
            len(done_cities),
            args.cities_done_file,
        )

    cities_run = 0

    for index, city in enumerate(cities, start=1):
        label = city or "(country-wide)"
        if args.all_cities and city in done_cities:
            LOGGER.info(
                "[%s/%s] Skip city already completed: %s",
                index,
                len(cities),
                label,
            )
            continue

        LOGGER.info("[%s/%s] Starting city=%s", index, len(cities), label)
        crawler = BehtarinoRepairShopCrawler(
            category=args.category,
            city=city,
            output_path=args.output,
            limit_pages=args.limit_pages,
            max_retries=args.max_retries,
            timeout=args.timeout,
            # Always resume when appending across cities / restarts.
            resume=True if args.all_cities else args.resume,
        )
        code = crawler.run()
        cities_run += 1

        if code == 0 and args.all_cities and city:
            _mark_city_done(args.cities_done_file, city)
            LOGGER.info("Marked city done: %s", city)
        elif code != 0:
            LOGGER.warning("City=%s finished with status=%s; continuing", label, code)

    LOGGER.info(
        "All cities finished. cities_run=%s output=%s",
        cities_run,
        args.output.resolve(),
    )
    return 0 if cities_run > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
