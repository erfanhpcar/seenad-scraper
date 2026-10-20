#!/usr/bin/env python3
"""
Two-phase crawler for Behtarino.com business directory (car dealers / نمایشگاه).

Phase 1: Paginate /r/{category}/{city}?page=N; extract LocalBusiness rows from
          SearchResultsPage JSON-LD embedded in SSR HTML.
Phase 2: Fetch each /p/{slug} detail page for phone numbers; append JSONL rows.

Default is Tehran-only (historical). Behtarino's country-wide /r/{category} ranking
is also Tehran-heavy. For Iran coverage use --all-cities and skip تهران.

Usage:
  python scripts/crawl_behtarino.py
  python scripts/crawl_behtarino.py --limit-pages 2 --output data/raw_behtarino_dealers.jsonl -v
  python scripts/crawl_behtarino.py --resume --output data/raw_behtarino_dealers.jsonl
  python scripts/crawl_behtarino.py --all-cities --skip-cities تهران --resume
  python scripts/crawl_behtarino.py --city اصفهان
  python scripts/crawl_behtarino.py --no-city --category نمایشگاه-خودرو

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
from bs4 import BeautifulSoup
from requests import Response
from requests.exceptions import RequestException, Timeout

LOGGER = logging.getLogger("behtarino-crawler")

BASE_URL = "https://behtarino.com"
DEFAULT_CATEGORY = "نمایشگاه-خودرو"
DEFAULT_CITY = "تهران"
DEFAULT_OUTPUT = Path("data/raw_behtarino_dealers.jsonl")
DEFAULT_CITIES_DONE = Path("data/behtarino_dealers_cities_done.txt")
CITY_HREF_RE = re.compile(r'href="(/r/[^"]+)"')
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_DELAY_MIN = 1.5
REQUEST_DELAY_MAX = 3.5

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


@dataclass(frozen=True)
class ListingBusiness:
    name: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    profile_url: str
    slug: str | None
    position: int | None
    listing_page: int
    listing_json_ld_item: dict[str, Any] = field(repr=False, default_factory=dict)
    street_address: str | None = None
    address_locality: str | None = None
    address_region: str | None = None


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


def _is_blocked_response(status: int, html: str) -> bool:
    if status in {403, 429, 503}:
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in ANTI_BOT_MARKERS)


def extract_search_results_json_ld(html: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return LocalBusiness-shaped items and the SearchResultsPage payload fragment."""
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
            if item_type not in {None, "LocalBusiness", "Organization"}:
                continue

            geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
            address = item.get("address") if isinstance(item.get("address"), dict) else {}
            profile_url = item.get("url")
            if not isinstance(profile_url, str) or not profile_url.strip():
                continue

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
                    "telephone": item.get("telephone"),
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
    json_ld_item = row.get("json_ld_item")
    if not isinstance(json_ld_item, dict):
        json_ld_item = {}

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
        listing_json_ld_item=json_ld_item,
    )


def extract_phones_from_detail_html(html: str) -> tuple[list[str], dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    phones: list[str] = []

    tel_links = [
        anchor["href"].replace("tel:", "").strip()
        for anchor in soup.select('a[href^="tel:"]')
        if anchor.get("href")
    ]
    phones.extend(tel_links)

    mobiles = MOBILE_PHONE_RE.findall(html)
    landlines = LANDLINE_PHONE_RE.findall(html)
    phones.extend(mobiles)
    phones.extend(landlines)

    json_ld_telephones: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for node in _iter_json_ld_nodes(payload):
            tel = node.get("telephone")
            if isinstance(tel, str) and tel.strip():
                json_ld_telephones.append(tel.strip())
            elif isinstance(tel, list):
                for entry in tel:
                    if isinstance(entry, str) and entry.strip():
                        json_ld_telephones.append(entry.strip())
    phones.extend(json_ld_telephones)

    telephone_literals = re.findall(r'"telephone"\s*:\s*"([^"]+)"', html)
    phones.extend(telephone_literals)

    normalized = _dedupe_phones(phones)
    audit = {
        "tel_links": tel_links[:10],
        "mobile_pattern_hits": sorted(set(mobiles))[:10],
        "landline_pattern_hits": sorted(set(landlines))[:10],
        "json_ld_telephones": json_ld_telephones[:10],
        "telephone_literals": telephone_literals[:10],
        "geo_in_html": [
            {"latitude": float(lat), "longitude": float(lng)}
            for lat, lng in GEO_JSON_RE.findall(html)[:3]
        ],
    }
    return normalized, audit


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


def _build_record(
    listing: ListingBusiness,
    *,
    category: str,
    city: str | None,
    phones: list[str],
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": "behtarino.com",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "city": city,
        "name": listing.name,
        "address": listing.address,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "profile_url": listing.profile_url,
        "slug": listing.slug,
        "phones": phones,
        "listing_page": listing.listing_page,
        "position": listing.position,
        "street_address": listing.street_address,
        "address_locality": listing.address_locality,
        "address_region": listing.address_region,
        "raw_payload": raw_payload,
    }


class BehtarinoCrawler:
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

            if status == 404:
                LOGGER.info("Listing page=%s returned 404; pagination complete", page)
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

            rows, page_payload = extract_search_results_json_ld(html)
            if not rows:
                LOGGER.info(
                    "No LocalBusiness items in JSON-LD on page=%s; pagination complete",
                    page,
                )
                break

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

            if page_payload is None:
                LOGGER.debug("SearchResultsPage JSON-LD missing on page=%s", page - 1)

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

            phones, phone_audit = extract_phones_from_detail_html(html)
            listing_tel = listing.listing_json_ld_item.get("telephone")
            if isinstance(listing_tel, str) and listing_tel.strip():
                phones = _dedupe_phones(phones + [listing_tel.strip()])

            raw_payload: dict[str, Any] = {
                "listing_json_ld_item": listing.listing_json_ld_item,
                "detail_phones_audit": phone_audit,
                "detail_html_length": len(html),
            }

            try:
                record = _build_record(
                    listing,
                    category=self.category,
                    city=self.city,
                    phones=phones,
                    raw_payload=raw_payload,
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
            "Collected %s unique business listing(s) from %s",
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
            "Crawl Behtarino.com dealer/showroom listings into JSONL. "
            "Prefer --all-cities for Iran coverage (default city is Tehran)."
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
        help=f"City slug (default if omitted: {DEFAULT_CITY})",
    )
    parser.add_argument(
        "--no-city",
        action="store_true",
        help="Use /r/{category} without a city segment",
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
        help="Stop listing phase after N pages (testing)",
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


def _bootstrap_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
        }
    )
    return session


def _resolve_cities(args: argparse.Namespace, session: requests.Session) -> list[str | None]:
    selected = sum(bool(x) for x in (args.all_cities, args.no_city, args.city))
    if selected > 1:
        raise SystemExit("Use only one of --city, --all-cities, or --no-city")

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

    if args.no_city:
        LOGGER.warning(
            "Crawling country-wide /r/%s (Behtarino often returns Tehran-heavy results)",
            args.category,
        )
        return [None]

    if isinstance(args.city, str) and args.city.strip():
        return [args.city.strip()]

    LOGGER.info("No --city / --all-cities: defaulting to city=%s", DEFAULT_CITY)
    return [DEFAULT_CITY]


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose)

    try:
        cities = _resolve_cities(args, _bootstrap_session())
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
        crawler = BehtarinoCrawler(
            category=args.category,
            city=city,
            output_path=args.output,
            limit_pages=args.limit_pages,
            max_retries=args.max_retries,
            timeout=args.timeout,
            resume=True if args.all_cities else args.resume,
        )
        code = crawler.run()
        cities_run += 1

        if code == 0 and args.all_cities and city:
            _mark_city_done(args.cities_done_file, city)
            LOGGER.info("Marked city done: %s", city)
        elif code != 0:
            LOGGER.warning("City=%s finished with status=%s; continuing", label, code)

    if args.all_cities:
        LOGGER.info(
            "All cities finished. cities_run=%s output=%s",
            cities_run,
            args.output.resolve(),
        )
        return 0 if cities_run > 0 else 1

    return 0 if cities_run > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
