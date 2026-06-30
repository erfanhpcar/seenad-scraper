#!/usr/bin/env python3
"""
Multi-brand raw-data crawler for Balad.ir branch dealership listings.

Hybrid strategy per brand (page 1 HTML + pagination API):
  1. Fetch branch HTML and extract full ``__NEXT_DATA__`` (rich SSR payload).
  2. Paginate ``search.raah.ir/v4/placeslist/branch/`` for pages 2..N.

Usage:
  python scripts/crawl_balad_dealerships.py
  python scripts/crawl_balad_dealerships.py --brand saipa -v
  python scripts/crawl_balad_dealerships.py --brand iran-khodro --slug tehran-iran-khodro-dealership
  python scripts/crawl_balad_dealerships.py --output-dir data/balad -v

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
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests import Response
from requests.exceptions import RequestException

LOGGER = logging.getLogger("balad-dealerships-crawler")

BALAD_BRANCH_BASE_URL = "https://balad.ir/branch"
DEFAULT_OUTPUT_DIR = Path("data/balad")
PLACESLIST_API_BASE = "https://search.raah.ir/v4/placeslist/branch/"
REQUEST_TIMEOUT_S = 30
BRAND_SLEEP_MIN_S = 3.0
BRAND_SLEEP_MAX_S = 6.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
NEXT_DATA_SCRIPT_ID = "__NEXT_DATA__"
NEXT_DATA_REGEX = re.compile(
    rf'<script id="{NEXT_DATA_SCRIPT_ID}"[^>]*>(.*?)</script>',
    re.DOTALL,
)


@dataclass(frozen=True)
class BrandTarget:
    """Logical brand key (output filename) mapped to an actual Balad branch slug."""

    key: str
    slug: str
    label: str = ""


# Priority queue: key is stable for output files; slug is the live Balad branch name.
DEFAULT_BRAND_TARGETS: tuple[BrandTarget, ...] = (
    BrandTarget(
        key="iran-khodro",
        slug="tehran-iran-khodro-dealership",
        label="ایران خودرو (تهران)",
    ),
    BrandTarget(
        key="saipa",
        slug="tehran-saipa-agency",
        label="سایپا (تهران)",
    ),
    BrandTarget(
        key="tehran-pars-khodro",
        slug="tehran-pars-khodro",
        label="پارس خودرو (تهران)",
    ),
)


@dataclass
class BrandCrawlResult:
    brand_key: str
    branch_slug: str
    output_path: Path
    payload: dict[str, Any] | None = None
    success: bool = False
    error: str | None = None


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
    def parse_json(text: str, *, label: str = "json") -> Any | None:
        return SafeField.run(
            lambda: json.loads(text),
            default=None,
            label=label,
            on_error=lambda exc: LOGGER.warning("JSON parse failed (%s): %s", label, exc),
        )

    @staticmethod
    def response_json(response: Response, *, label: str = "response") -> dict[str, Any] | None:
        payload = SafeField.run(
            response.json,
            default=None,
            label=f"{label}.json",
            on_error=lambda exc: LOGGER.warning("Response JSON decode failed (%s): %s", label, exc),
        )
        return payload if isinstance(payload, dict) else None


def build_branch_url(branch_slug: str) -> str:
    return f"{BALAD_BRANCH_BASE_URL}/{branch_slug.strip('/')}"


def brand_output_path(output_dir: Path, brand_key: str) -> Path:
    return output_dir / f"{brand_key}_raw.json"


def resolve_brand_targets(
    *,
    brand_key: str | None,
    slug_override: str | None,
    extra_slugs: list[str] | None,
) -> list[BrandTarget]:
    if extra_slugs:
        return [
            BrandTarget(key=slug, slug=slug, label=slug)
            for slug in extra_slugs
        ]

    targets = list(DEFAULT_BRAND_TARGETS)

    if brand_key is not None:
        matched = next((target for target in targets if target.key == brand_key), None)
        if matched is None:
            slug = slug_override or brand_key
            matched = BrandTarget(key=brand_key, slug=slug, label=brand_key)
        elif slug_override is not None:
            matched = BrandTarget(key=matched.key, slug=slug_override, label=matched.label)
        return [matched]

    if slug_override is not None:
        raise ValueError("--slug requires --brand when not using --slug-list")

    return targets


@dataclass
class HttpClient:
    """Session-aware HTTP client with Balad-compatible headers."""

    referer: str
    timeout_s: float = REQUEST_TIMEOUT_S
    session: requests.Session = field(init=False)

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": self.referer,
            }
        )

    def get_html(self, url: str) -> tuple[Response | None, str]:
        response = self._request("GET", url, headers={"Accept": "text/html,application/xhtml+xml"})
        if response is None:
            return None, ""
        html = SafeField.run(
            lambda: response.text,
            default="",
            label="response.text",
            on_error=lambda exc: LOGGER.error("Failed to read HTML body from %s: %s", url, exc),
        )
        return response, html

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[Response | None, dict[str, Any] | None]:
        headers = {"Accept": "application/json", "Origin": "https://balad.ir"}
        if extra_headers:
            headers.update(extra_headers)
        response = self._request("GET", url, params=params, headers=headers)
        if response is None:
            return None, None
        payload = SafeField.response_json(response, label=url)
        return response, payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response | None:
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            return response
        except RequestException as exc:
            LOGGER.error("HTTP %s %s failed: %s", method, url, exc)
            return None


class NextDataParser:
    """Extract and parse ``__NEXT_DATA__`` and ancillary HTML payloads."""

    def __init__(self, html: str) -> None:
        self._html = html

    def extract_next_data(self) -> dict[str, Any] | None:
        regex_payload = self._extract_via_regex()
        if regex_payload is not None:
            return regex_payload
        return self._extract_via_soup()

    def extract_json_ld_blocks(self) -> list[Any]:
        blocks: list[Any] = []
        soup = SafeField.run(
            lambda: BeautifulSoup(self._html, "html.parser"),
            default=None,
            label="beautifulsoup.html",
        )
        if soup is None:
            return blocks

        for script in soup.find_all("script", type="application/ld+json"):
            raw_text = script.string or script.get_text()
            if not raw_text or not raw_text.strip():
                continue
            payload = SafeField.parse_json(raw_text.strip(), label="json_ld")
            if payload is not None:
                blocks.append(payload)
        return blocks

    def page_count(self, next_data: dict[str, Any] | None) -> int | None:
        if not next_data:
            return None
        value = SafeField.dig(next_data, "props", "pageProps", "data", "pageCount")
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _extract_via_regex(self) -> dict[str, Any] | None:
        match = NEXT_DATA_REGEX.search(self._html)
        if not match:
            return None
        payload = SafeField.parse_json(match.group(1), label="__NEXT_DATA__ (regex)")
        return payload if isinstance(payload, dict) else None

    def _extract_via_soup(self) -> dict[str, Any] | None:
        soup = SafeField.run(
            lambda: BeautifulSoup(self._html, "html.parser"),
            default=None,
            label="beautifulsoup.next_data",
        )
        if soup is None:
            return None

        script = soup.find("script", id=NEXT_DATA_SCRIPT_ID)
        if script is None:
            content_match = re.search(
                rf"{re.escape(NEXT_DATA_SCRIPT_ID)}.*?>(.*?)</script>",
                self._html,
                re.DOTALL,
            )
            if not content_match:
                return None
            raw_text = content_match.group(1)
        else:
            raw_text = script.string or script.get_text()

        if not raw_text or not raw_text.strip():
            return None

        payload = SafeField.parse_json(raw_text.strip(), label="__NEXT_DATA__ (soup)")
        return payload if isinstance(payload, dict) else None


@dataclass
class BaladApiPagination:
    """Fetch paginated branch listing pages from search.raah.ir."""

    http_client: HttpClient
    branch_slug: str
    source_url: str
    api_base_url: str = PLACESLIST_API_BASE

    def fetch_page(self, page: int) -> dict[str, Any]:
        request_url = f"{self.api_base_url.rstrip('/')}/{self.branch_slug}"
        params = {"name": self.branch_slug, "page": page}
        response, payload = self.http_client.get_json(
            self.api_base_url,
            params=params,
            extra_headers={
                "platform": "web",
                "Referer": self.source_url,
            },
        )

        return {
            "page": page,
            "endpoint": self.api_base_url,
            "endpoint_with_slug": request_url,
            "request_params": params,
            "status_code": response.status_code if response is not None else None,
            "raw": payload,
        }

    def fetch_remaining_pages(
        self,
        *,
        start_page: int,
        page_count: int,
    ) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for page in range(start_page, page_count + 1):
            LOGGER.info("[%s] Fetching API page %s/%s", self.branch_slug, page, page_count)
            page_payload = self.fetch_page(page)
            responses.append(page_payload)

            api_page_count = self._resolve_page_count(page_payload.get("raw"))
            if api_page_count is not None and api_page_count != page_count:
                LOGGER.debug(
                    "[%s] API reported page_count=%s (initial=%s) on page %s",
                    self.branch_slug,
                    api_page_count,
                    page_count,
                    page,
                )
        return responses

    @staticmethod
    def _resolve_page_count(raw_payload: Any) -> int | None:
        if not isinstance(raw_payload, dict):
            return None
        for key in ("page_count", "pageCount"):
            value = raw_payload.get(key)
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None


@dataclass
class BaladDealershipCrawler:
    """Orchestrate HTML first-page extraction and API pagination for one branch slug."""

    branch_slug: str
    source_url: str = field(init=False)
    brand_key: str | None = None
    http_client: HttpClient = field(init=False)

    def __post_init__(self) -> None:
        self.branch_slug = self.branch_slug.strip("/")
        if not self.branch_slug:
            raise ValueError("branch_slug must not be empty")
        self.source_url = build_branch_url(self.branch_slug)
        self.http_client = HttpClient(referer=self.source_url)

    def crawl(self) -> dict[str, Any]:
        crawled_at = datetime.now(timezone.utc).isoformat()
        LOGGER.info(
            "Crawling Balad branch: %s (slug=%s, brand_key=%s)",
            self.source_url,
            self.branch_slug,
            self.brand_key or self.branch_slug,
        )

        first_page = self._crawl_first_page()
        page_count = first_page.get("page_count")

        pagination_responses: list[dict[str, Any]] = []
        if isinstance(page_count, int) and page_count > 1:
            paginator = BaladApiPagination(
                http_client=self.http_client,
                branch_slug=self.branch_slug,
                source_url=self.source_url,
            )
            pagination_responses = paginator.fetch_remaining_pages(
                start_page=2,
                page_count=page_count,
            )
        elif page_count is None:
            LOGGER.warning("[%s] page_count unavailable; probing API from page 2", self.branch_slug)
            paginator = BaladApiPagination(
                http_client=self.http_client,
                branch_slug=self.branch_slug,
                source_url=self.source_url,
            )
            probe = paginator.fetch_page(2)
            pagination_responses = [probe] if probe.get("raw") is not None else []
            discovered = BaladApiPagination._resolve_page_count(probe.get("raw"))
            if discovered and discovered > 2:
                pagination_responses.extend(
                    paginator.fetch_remaining_pages(start_page=3, page_count=discovered)
                )

        return {
            "crawl_metadata": {
                "brand_key": self.brand_key or self.branch_slug,
                "source_url": self.source_url,
                "branch_slug": self.branch_slug,
                "crawled_at": crawled_at,
                "page_count": page_count,
                "status": "success",
                "methods": {
                    "first_page": "html___NEXT_DATA__",
                    "pagination": "search.raah.ir_placeslist_branch_api",
                },
                "pagination_api_base": PLACESLIST_API_BASE,
                "pagination_api_slug_endpoint": (
                    f"{PLACESLIST_API_BASE.rstrip('/')}/{self.branch_slug}"
                ),
            },
            "first_page": first_page,
            "pagination_responses": pagination_responses,
        }

    def _crawl_first_page(self) -> dict[str, Any]:
        response, html = self.http_client.get_html(self.source_url)
        parser = NextDataParser(html)
        next_data = parser.extract_next_data()
        json_ld_blocks = parser.extract_json_ld_blocks()
        page_count = parser.page_count(next_data)

        if next_data is None:
            LOGGER.warning("[%s] __NEXT_DATA__ not found on first page", self.branch_slug)

        item_count = SafeField.dig(next_data, "props", "pageProps", "data", "items")
        if isinstance(item_count, list):
            LOGGER.info("[%s] First page __NEXT_DATA__ items: %s", self.branch_slug, len(item_count))
        LOGGER.info("[%s] Detected page_count: %s", self.branch_slug, page_count)

        return {
            "url": self.source_url,
            "status_code": response.status_code if response is not None else None,
            "byte_size": len(html.encode("utf-8")) if html else 0,
            "page_count": page_count,
            "__NEXT_DATA__": next_data,
            "json_ld": json_ld_blocks,
        }


@dataclass
class BaladMultiBrandCrawler:
    """Run the full pipeline across multiple brand targets with isolation and backoff."""

    brand_targets: list[BrandTarget]
    output_dir: Path
    sleep_min_s: float = BRAND_SLEEP_MIN_S
    sleep_max_s: float = BRAND_SLEEP_MAX_S

    def crawl_all(self) -> list[BrandCrawlResult]:
        results: list[BrandCrawlResult] = []
        total = len(self.brand_targets)

        for index, target in enumerate(self.brand_targets):
            output_path = brand_output_path(self.output_dir, target.key)
            LOGGER.info(
                "=== Brand %s/%s: %s (slug=%s) ===",
                index + 1,
                total,
                target.label or target.key,
                target.slug,
            )

            result = self._crawl_brand_safe(target, output_path)
            results.append(result)

            if result.success and result.payload is not None:
                _write_output(result.payload, output_path)
            elif result.payload is not None:
                _write_output(result.payload, output_path)

            if index < total - 1:
                delay_s = random.uniform(self.sleep_min_s, self.sleep_max_s)
                LOGGER.info("Sleeping %.1fs before next brand", delay_s)
                time.sleep(delay_s)

        return results

    def _crawl_brand_safe(self, target: BrandTarget, output_path: Path) -> BrandCrawlResult:
        def _execute() -> dict[str, Any]:
            crawler = BaladDealershipCrawler(
                branch_slug=target.slug,
                brand_key=target.key,
            )
            return crawler.crawl()

        payload = SafeField.run(
            _execute,
            default=None,
            label=f"brand:{target.key}",
            on_error=lambda exc: LOGGER.error(
                "Brand crawl failed for %s (slug=%s): %s",
                target.key,
                target.slug,
                exc,
            ),
        )

        if payload is not None:
            return BrandCrawlResult(
                brand_key=target.key,
                branch_slug=target.slug,
                output_path=output_path,
                payload=payload,
                success=True,
            )

        failed_payload = {
            "crawl_metadata": {
                "brand_key": target.key,
                "source_url": build_branch_url(target.slug),
                "branch_slug": target.slug,
                "crawled_at": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "error": f"Crawl aborted for brand '{target.key}' (slug={target.slug})",
            },
            "first_page": None,
            "pagination_responses": [],
        }
        return BrandCrawlResult(
            brand_key=target.key,
            branch_slug=target.slug,
            output_path=output_path,
            payload=failed_payload,
            success=False,
            error=failed_payload["crawl_metadata"]["error"],
        )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _write_output(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    output_path.write_text(serialized, encoding="utf-8")
    LOGGER.info("Wrote raw payload to %s (%s bytes)", output_path, len(serialized.encode("utf-8")))


def _log_run_summary(results: list[BrandCrawlResult]) -> None:
    succeeded = sum(1 for result in results if result.success)
    failed = len(results) - succeeded
    LOGGER.info("Run complete — brands_ok=%s brands_failed=%s total=%s", succeeded, failed, len(results))
    for result in results:
        status = "OK" if result.success else "FAIL"
        first_items = SafeField.dig(
            result.payload,
            "first_page",
            "__NEXT_DATA__",
            "props",
            "pageProps",
            "data",
            "items",
            default=[],
        )
        pagination_pages = len(
            SafeField.dig(result.payload, "pagination_responses", default=[]) or []
        )
        LOGGER.info(
            "  [%s] %s — first_page_items=%s pagination_pages=%s -> %s",
            status,
            result.brand_key,
            len(first_items) if isinstance(first_items, list) else 0,
            pagination_pages,
            result.output_path,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl raw Balad.ir branch dealership data for multiple automaker brands "
            "(__NEXT_DATA__ + raah.ir API)."
        )
    )
    parser.add_argument(
        "--brand",
        help=(
            "Crawl a single brand by logical key "
            f"({', '.join(target.key for target in DEFAULT_BRAND_TARGETS)})"
        ),
    )
    parser.add_argument(
        "--slug",
        help="Override the Balad branch slug for --brand (e.g. tehran-saipa-agency)",
    )
    parser.add_argument(
        "--slug-list",
        nargs="+",
        metavar="SLUG",
        help="Ad-hoc list of Balad branch slugs; each slug is used as both key and slug",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for per-brand JSON files (default: {DEFAULT_OUTPUT_DIR})",
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

    try:
        brand_targets = resolve_brand_targets(
            brand_key=args.brand,
            slug_override=args.slug,
            extra_slugs=args.slug_list,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    orchestrator = BaladMultiBrandCrawler(
        brand_targets=brand_targets,
        output_dir=args.output_dir,
    )
    results = orchestrator.crawl_all()
    _log_run_summary(results)

    return 0 if all(result.success for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
