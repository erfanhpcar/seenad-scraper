#!/usr/bin/env python3
"""
Nationwide geospatial search crawler for automotive service points on Balad.ir.

Collects raw dealership and repair-shop POIs across Iran using a geo-grid of
city anchor points, keyword queries, and paginated search API responses.

Usage:
  python scripts/crawl_balad_global_search.py
  python scripts/crawl_balad_global_search.py --city tehran -v
  python scripts/crawl_balad_global_search.py --output data/balad/global_automotive_raw.json

Dependencies (project requirements.txt):
  requests
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
from requests import Response
from requests.exceptions import RequestException

LOGGER = logging.getLogger("balad-global-search-crawler")

DEFAULT_OUTPUT = Path("data/balad/global_automotive_raw.json")
PRIMARY_SEARCH_URL = "https://api.balad.ir/v4/search"
FALLBACK_SEARCH_URL = "https://search.raah.ir/v4/submit/"
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "نمایندگی خودرو",
    "تعمیرگاه مجاز",
    "مکانیکی",
)
REQUEST_TIMEOUT_S = 30
REQUEST_SLEEP_MIN_S = 2.0
REQUEST_SLEEP_MAX_S = 4.0
MAX_PAGINATION_PAGES = 10
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class GeoZone:
    zone_id: str
    city_key: str
    label: str
    latitude: float
    longitude: float
    polygon: str | None = None
    zoom: int = 12


class SafeField:
    """Isolated try/except helpers so one bad field never crashes the pipeline."""

    @staticmethod
    def run(
        operation: Callable[[], Any],
        *,
        default: Any = None,
        label: str = "operation",
        on_error: Callable[[Exception], None] | None = None,
    ) -> Any:
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 — intentional production guard
            if on_error is not None:
                on_error(exc)
            else:
                LOGGER.debug("SafeField.%s failed: %s", label, exc)
            return default

    @staticmethod
    def dig(mapping: Any, *keys: str, default: Any = None) -> Any:
        current = mapping
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
        return current if current is not None else default

    @staticmethod
    def response_json(response: Response, *, label: str = "response") -> Any | None:
        return SafeField.run(
            response.json,
            default=None,
            label=f"{label}.json",
            on_error=lambda exc: LOGGER.warning("JSON decode failed (%s): %s", label, exc),
        )


class GeoGridManager:
    """City and sub-city anchor points for geospatial Balad search coverage."""

    TEHRAN_POLYGON = (
        "51.07034785986406,35.92586978405022|"
        "51.75356014013795,35.92586978405022|"
        "51.75356014013795,35.44120656173692|"
        "51.07034785986406,35.44120656173692|"
        "51.07034785986406,35.92586978405022"
    )

    def __init__(self) -> None:
        self._zones: tuple[GeoZone, ...] = self._build_zones()

    def all_zones(self) -> list[GeoZone]:
        return list(self._zones)

    def zones_for_city(self, city_key: str) -> list[GeoZone]:
        normalized = city_key.strip().lower()
        zones = [zone for zone in self._zones if zone.city_key == normalized]
        if not zones:
            available = sorted({zone.city_key for zone in self._zones})
            raise ValueError(
                f"Unknown city '{city_key}'. Available: {', '.join(available)}"
            )
        return zones

    def city_keys(self) -> list[str]:
        return sorted({zone.city_key for zone in self._zones})

    @staticmethod
    def _zone(
        zone_id: str,
        city_key: str,
        label: str,
        latitude: float,
        longitude: float,
        *,
        polygon: str | None = None,
        zoom: int = 12,
    ) -> GeoZone:
        return GeoZone(
            zone_id=zone_id,
            city_key=city_key,
            label=label,
            latitude=latitude,
            longitude=longitude,
            polygon=polygon,
            zoom=zoom,
        )

    def _build_zones(self) -> tuple[GeoZone, ...]:
        return (
            self._zone("tehran-center", "tehran", "تهران — مرکز", 35.6892, 51.3890, polygon=self.TEHRAN_POLYGON, zoom=10),
            self._zone("tehran-north", "tehran", "تهران — شمال", 35.7800, 51.4300, polygon=self.TEHRAN_POLYGON, zoom=11),
            self._zone("tehran-south", "tehran", "تهران — جنوب", 35.6000, 51.4100, polygon=self.TEHRAN_POLYGON, zoom=11),
            self._zone("tehran-east", "tehran", "تهران — شرق", 35.7200, 51.5500, polygon=self.TEHRAN_POLYGON, zoom=11),
            self._zone("tehran-west", "tehran", "تهران — غرب", 35.7000, 51.2500, polygon=self.TEHRAN_POLYGON, zoom=11),
            self._zone("mashhad-center", "mashhad", "مشهد", 36.2970, 59.6062),
            self._zone("isfahan-center", "isfahan", "اصفهان", 32.6539, 51.6660),
            self._zone("shiraz-center", "shiraz", "شیراز", 29.5918, 52.5837),
            self._zone("tabriz-center", "tabriz", "تبریز", 38.0800, 46.2919),
            self._zone("ahvaz-center", "ahvaz", "اهواز", 31.3183, 48.6706),
            self._zone("karaj-center", "karaj", "کرج", 35.8400, 50.9391),
            self._zone("qom-center", "qom", "قم", 34.6416, 50.8746),
            self._zone("kermanshah-center", "kermanshah", "کرمانشاه", 34.3142, 47.0650),
            self._zone("rasht-center", "rasht", "رشت", 37.2808, 49.5832),
            self._zone("yazd-center", "yazd", "یزد", 31.8974, 54.3569),
            self._zone("bandar-abbas-center", "bandar-abbas", "بندرعباس", 27.1832, 56.2666),
            self._zone("kerman-center", "kerman", "کرمان", 30.2839, 57.0834),
            self._zone("zahedan-center", "zahedan", "زاهدان", 29.4963, 60.8629),
            self._zone("arak-center", "arak", "اراک", 34.0917, 49.6892),
            self._zone("hamadan-center", "hamadan", "همدان", 34.7992, 48.5146),
            self._zone("urmia-center", "urmia", "ارومیه", 37.5553, 45.0725),
            self._zone("sari-center", "sari", "ساری", 36.5633, 53.0601),
        )


@dataclass
class BaladSearchApi:
    """HTTP client for Balad geospatial search (primary + resilient fallback)."""

    referer: str = "https://balad.ir/search"
    timeout_s: float = REQUEST_TIMEOUT_S
    session: requests.Session = field(init=False)

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "https://balad.ir",
                "Referer": self.referer,
                "platform": "web",
            }
        )

    def search(
        self,
        *,
        q: str,
        latitude: float,
        longitude: float,
        page: int = 1,
        polygon: str | None = None,
        zoom: int | None = None,
        paging_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        primary_params = {
            "q": q,
            "latitude": latitude,
            "longitude": longitude,
            "page": page,
        }
        primary_response, primary_payload = self._request_json(
            PRIMARY_SEARCH_URL,
            params=primary_params,
        )
        if self._is_valid_search_payload(primary_payload):
            return {
                "transport": "primary",
                "endpoint": PRIMARY_SEARCH_URL,
                "request_params": primary_params,
                "status_code": primary_response.status_code if primary_response else None,
                "raw": primary_payload,
            }

        fallback_params: dict[str, Any] = {
            "text": q,
            "q": q,
            "latitude": latitude,
            "longitude": longitude,
            "camera": f"{longitude},{latitude}",
            "page": page,
        }
        if polygon:
            fallback_params["polygon"] = polygon
        if zoom is not None:
            fallback_params["zoom"] = zoom
        if paging_meta:
            fallback_params.update(paging_meta)

        fallback_response, fallback_payload = self._request_json(
            FALLBACK_SEARCH_URL,
            params=fallback_params,
        )
        return {
            "transport": "fallback_submit",
            "endpoint": FALLBACK_SEARCH_URL,
            "request_params": fallback_params,
            "status_code": fallback_response.status_code if fallback_response else None,
            "raw": fallback_payload,
            "primary_probe": {
                "endpoint": PRIMARY_SEARCH_URL,
                "request_params": primary_params,
                "status_code": primary_response.status_code if primary_response else None,
                "raw": primary_payload,
            },
        }

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
    ) -> tuple[Response | None, Any | None]:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout_s)
            response.raise_for_status()
        except RequestException as exc:
            LOGGER.error("Search request failed (%s): %s", url, exc)
            return None, None

        if not response.text or not response.text.strip():
            return response, None

        stripped = response.text.strip()
        if stripped.isdigit() and len(stripped) <= 4:
            return response, stripped

        payload = SafeField.response_json(response, label=url)
        if payload is not None:
            return response, payload

        return response, stripped

    @staticmethod
    def _is_valid_search_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("poi-tokens") or payload.get("geojson"):
            return True
        for key in ("items", "results", "places", "data"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return True
        return False


class RawDataDeduplicator:
    """Deduplicate Balad POIs by token / place_id while preserving first-seen raw payload."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def ingest_search_response(
        self,
        *,
        search_result: dict[str, Any],
        keyword: str,
        zone: GeoZone,
        page: int,
    ) -> int:
        raw_payload = search_result.get("raw")
        if not isinstance(raw_payload, dict):
            return 0

        new_count = 0
        tokens = raw_payload.get("poi-tokens")
        token_list = tokens if isinstance(tokens, list) else []

        features = SafeField.dig(raw_payload, "geojson", "features", default=[])
        if not isinstance(features, list):
            features = []

        if token_list and features and len(token_list) == len(features):
            for index, token in enumerate(token_list):
                feature = features[index] if index < len(features) else None
                if self._add_record(
                    dedupe_key=str(token),
                    token=str(token),
                    place_id=None,
                    geojson_feature=feature,
                    keyword=keyword,
                    zone=zone,
                    page=page,
                    search_result=search_result,
                    raw_payload=raw_payload,
                ):
                    new_count += 1
            return new_count

        list_candidates = (
            raw_payload.get("items"),
            raw_payload.get("results"),
            raw_payload.get("places"),
            raw_payload.get("data"),
        )
        for candidate in list_candidates:
            if not isinstance(candidate, list):
                continue
            for item in candidate:
                if not isinstance(item, dict):
                    continue
                dedupe_key = self._extract_dedupe_key(item)
                if dedupe_key is None:
                    continue
                if self._add_record(
                    dedupe_key=dedupe_key,
                    token=item.get("token"),
                    place_id=item.get("place_id") or item.get("id"),
                    geojson_feature=None,
                    keyword=keyword,
                    zone=zone,
                    page=page,
                    search_result=search_result,
                    raw_payload=raw_payload,
                    raw_item=item,
                ):
                    new_count += 1
            return new_count

        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            token = token_list[index] if index < len(token_list) else None
            dedupe_key = str(token) if token else self._feature_fallback_key(feature)
            if self._add_record(
                dedupe_key=dedupe_key,
                token=token,
                place_id=SafeField.dig(feature, "properties", "place_id"),
                geojson_feature=feature,
                keyword=keyword,
                zone=zone,
                page=page,
                search_result=search_result,
                raw_payload=raw_payload,
            ):
                new_count += 1

        return new_count

    def unique_records(self) -> list[dict[str, Any]]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def _add_record(
        self,
        *,
        dedupe_key: str,
        token: Any,
        place_id: Any,
        geojson_feature: Any,
        keyword: str,
        zone: GeoZone,
        page: int,
        search_result: dict[str, Any],
        raw_payload: dict[str, Any],
        raw_item: dict[str, Any] | None = None,
    ) -> bool:
        if not dedupe_key or dedupe_key in self._records:
            return False

        discovery_context = {
            "keyword": keyword,
            "city_key": zone.city_key,
            "zone_id": zone.zone_id,
            "zone_label": zone.label,
            "latitude": zone.latitude,
            "longitude": zone.longitude,
            "page": page,
            "transport": search_result.get("transport"),
            "endpoint": search_result.get("endpoint"),
        }

        record: dict[str, Any] = {
            "dedupe_key": dedupe_key,
            "token": token,
            "place_id": place_id,
            "discovery_context": discovery_context,
            "search_submit_raw": raw_payload,
        }
        if geojson_feature is not None:
            record["geojson_feature"] = geojson_feature
        if raw_item is not None:
            record["raw_item"] = raw_item

        self._records[dedupe_key] = record
        return True

    @staticmethod
    def _extract_dedupe_key(item: dict[str, Any]) -> str | None:
        for key in ("token", "place_id", "id", "poi_id"):
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value)
        return None

    @staticmethod
    def _feature_fallback_key(feature: dict[str, Any]) -> str:
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        coordinates = geometry.get("coordinates") if isinstance(geometry.get("coordinates"), list) else []
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        name = properties.get("name", "")
        lng = coordinates[0] if len(coordinates) > 0 else ""
        lat = coordinates[1] if len(coordinates) > 1 else ""
        return f"{name}|{lng}|{lat}"


@dataclass
class BaladGlobalOrchestrator:
    """Coordinate keyword × geo-grid × pagination loops with safe error isolation."""

    keywords: tuple[str, ...]
    zones: list[GeoZone]
    output_path: Path
    sleep_min_s: float = REQUEST_SLEEP_MIN_S
    sleep_max_s: float = REQUEST_SLEEP_MAX_S

    def crawl(self) -> dict[str, Any]:
        crawled_at = datetime.now(timezone.utc).isoformat()
        search_api = BaladSearchApi()
        deduplicator = RawDataDeduplicator()
        raw_search_responses: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        total_jobs = len(self.keywords) * len(self.zones)
        job_index = 0

        LOGGER.info(
            "Starting global Balad search — keywords=%s zones=%s",
            len(self.keywords),
            len(self.zones),
        )

        for keyword in self.keywords:
            for zone in self.zones:
                job_index += 1
                LOGGER.info(
                    "Job %s/%s — keyword='%s' zone=%s (%s)",
                    job_index,
                    total_jobs,
                    keyword,
                    zone.zone_id,
                    zone.label,
                )

                job_result = SafeField.run(
                    lambda keyword=keyword, zone=zone: self._crawl_zone_keyword(
                        search_api=search_api,
                        keyword=keyword,
                        zone=zone,
                        deduplicator=deduplicator,
                        raw_search_responses=raw_search_responses,
                    ),
                    default=None,
                    label=f"zone_keyword:{zone.zone_id}:{keyword}",
                    on_error=lambda exc, keyword=keyword, zone=zone: LOGGER.error(
                        "Zone/keyword crawl failed (%s / %s): %s",
                        zone.zone_id,
                        keyword,
                        exc,
                    ),
                )

                if job_result is None:
                    errors.append(
                        {
                            "keyword": keyword,
                            "zone_id": zone.zone_id,
                            "city_key": zone.city_key,
                            "error": f"Crawl aborted for zone '{zone.zone_id}' and keyword '{keyword}'",
                        }
                    )

                if job_index < total_jobs:
                    self._sleep()

        payload = {
            "crawl_metadata": {
                "crawled_at": crawled_at,
                "keywords": list(self.keywords),
                "zones": [
                    {
                        "zone_id": zone.zone_id,
                        "city_key": zone.city_key,
                        "label": zone.label,
                        "latitude": zone.latitude,
                        "longitude": zone.longitude,
                    }
                    for zone in self.zones
                ],
                "primary_search_endpoint": PRIMARY_SEARCH_URL,
                "fallback_search_endpoint": FALLBACK_SEARCH_URL,
                "unique_record_count": len(deduplicator),
                "raw_response_count": len(raw_search_responses),
                "error_count": len(errors),
            },
            "raw_search_responses": raw_search_responses,
            "unique_records": deduplicator.unique_records(),
            "errors": errors,
        }

        self._write_output(payload)
        return payload

    def _crawl_zone_keyword(
        self,
        *,
        search_api: BaladSearchApi,
        keyword: str,
        zone: GeoZone,
        deduplicator: RawDataDeduplicator,
        raw_search_responses: list[dict[str, Any]],
    ) -> int:
        new_records = 0
        seen_tokens: set[str] = set()
        paging_meta: dict[str, Any] | None = None

        for page in range(1, MAX_PAGINATION_PAGES + 1):
            search_result = search_api.search(
                q=keyword,
                latitude=zone.latitude,
                longitude=zone.longitude,
                page=page,
                polygon=zone.polygon,
                zoom=zone.zoom,
                paging_meta=paging_meta,
            )

            raw_payload = search_result.get("raw")
            if not isinstance(raw_payload, dict):
                LOGGER.warning(
                    "Empty/invalid search payload for %s / %s page %s",
                    zone.zone_id,
                    keyword,
                    page,
                )
                break

            raw_search_responses.append(
                {
                    "keyword": keyword,
                    "zone": {
                        "zone_id": zone.zone_id,
                        "city_key": zone.city_key,
                        "label": zone.label,
                        "latitude": zone.latitude,
                        "longitude": zone.longitude,
                    },
                    "page": page,
                    "transport": search_result.get("transport"),
                    "endpoint": search_result.get("endpoint"),
                    "request_params": search_result.get("request_params"),
                    "status_code": search_result.get("status_code"),
                    "primary_probe": search_result.get("primary_probe"),
                    "raw": raw_payload,
                }
            )

            before_count = len(deduplicator)
            deduplicator.ingest_search_response(
                search_result=search_result,
                keyword=keyword,
                zone=zone,
                page=page,
            )
            page_new = len(deduplicator) - before_count
            new_records += page_new

            tokens = raw_payload.get("poi-tokens")
            token_set = {str(token) for token in tokens} if isinstance(tokens, list) else set()
            if page > 1 and (not token_set or token_set.issubset(seen_tokens)):
                LOGGER.debug(
                    "Pagination exhausted for %s / %s at page %s",
                    zone.zone_id,
                    keyword,
                    page,
                )
                break

            seen_tokens.update(token_set)
            next_meta = raw_payload.get("bundle_paging_meta")
            if not isinstance(next_meta, dict) or not next_meta:
                break

            paging_meta = next_meta
            self._sleep()

        LOGGER.info(
            "Zone %s / '%s' — new_unique=%s total_unique=%s",
            zone.zone_id,
            keyword,
            new_records,
            len(deduplicator),
        )
        return new_records

    def _sleep(self) -> None:
        delay_s = random.uniform(self.sleep_min_s, self.sleep_max_s)
        LOGGER.debug("Sleeping %.1fs", delay_s)
        time.sleep(delay_s)

    def _write_output(self, payload: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        self.output_path.write_text(serialized, encoding="utf-8")
        LOGGER.info(
            "Wrote global search payload to %s (%s bytes, %s unique records)",
            self.output_path,
            len(serialized.encode("utf-8")),
            payload["crawl_metadata"]["unique_record_count"],
        )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    geo_manager = GeoGridManager()
    parser = argparse.ArgumentParser(
        description="Crawl nationwide Balad automotive service points via geospatial search."
    )
    parser.add_argument(
        "--city",
        help=f"Limit crawl to one city key. Available: {', '.join(geo_manager.city_keys())}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)

    geo_manager = GeoGridManager()
    try:
        zones = (
            geo_manager.zones_for_city(args.city)
            if args.city
            else geo_manager.all_zones()
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    orchestrator = BaladGlobalOrchestrator(
        keywords=DEFAULT_KEYWORDS,
        zones=zones,
        output_path=args.output,
    )
    payload = orchestrator.crawl()

    LOGGER.info(
        "Done — unique_records=%s raw_responses=%s errors=%s",
        payload["crawl_metadata"]["unique_record_count"],
        payload["crawl_metadata"]["raw_response_count"],
        payload["crawl_metadata"]["error_count"],
    )
    return 0 if payload["crawl_metadata"]["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
