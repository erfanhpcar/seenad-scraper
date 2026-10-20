"""
Khodro45 daily market prices (full catalogue on https://khodro45.com/pricing/).

Data source (same as the pricing frontend):
  GET https://khodro45.com/api/v1/pricing/dailycars/?limit=&offset=

Each ``items[]`` entry is one trim-year line with market / company / agency prices.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator, Mapping

import requests

LOGGER: Final = logging.getLogger(__name__)

PRICING_PAGE_URL: Final[str] = "https://khodro45.com/pricing/"
DAILYCARS_API: Final[str] = "https://khodro45.com/api/v1/pricing/dailycars/"
DEFAULT_PAGE_LIMIT: Final[int] = 50
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 45.0
DEFAULT_HTTP_MAX_ATTEMPTS: Final[int] = 5
RETRYABLE_HTTP_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Khodro45PricingRunSummary:
    brands_seen: int
    items_seen: int
    items_with_market_price: int
    pages_fetched: int
    output_path: str | None
    errors_http: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "khodro45",
            "pricing_page": PRICING_PAGE_URL,
            "api": DAILYCARS_API,
            "brands_seen": self.brands_seen,
            "items_seen": self.items_seen,
            "items_with_market_price": self.items_with_market_price,
            "pages_fetched": self.pages_fetched,
            "output_path": self.output_path,
            "errors_http": self.errors_http,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get_json(session: requests.Session, url: str) -> Mapping[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, DEFAULT_HTTP_MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=DEFAULT_REQUEST_TIMEOUT_S)
            if response.status_code in RETRYABLE_HTTP_STATUS:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("expected JSON object")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt >= DEFAULT_HTTP_MAX_ATTEMPTS:
                break
            delay = min(2.0 ** attempt + random.random(), 30.0)
            LOGGER.warning("khodro45 pricing GET retry %s/%s url=%s err=%s", attempt, DEFAULT_HTTP_MAX_ATTEMPTS, url, exc)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def iter_dailycars_pages(
    session: requests.Session,
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> Iterator[Mapping[str, Any]]:
    offset = 0
    while True:
        url = f"{DAILYCARS_API}?limit={page_limit}&offset={offset}"
        page = _http_get_json(session, url)
        yield page
        if not page.get("next"):
            break
        offset += page_limit


def flatten_dailycar_items(pages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scraped_at = _utc_now_iso()
    for page in pages:
        for brand_block in page.get("results") or []:
            brand = brand_block.get("brand") or {}
            for model_block in brand_block.get("models") or []:
                model = model_block.get("model") or {}
                for item in model_block.get("items") or []:
                    trimyear = item.get("trimyear") or {}
                    trim = trimyear.get("trim") or {}
                    year = trimyear.get("year") or {}
                    option = trimyear.get("option") or {}
                    nested_model = trim.get("model") or {}
                    nested_brand = nested_model.get("brand") or {}
                    prices = item.get("prices") or {}
                    market = prices.get("market") or {}
                    company = prices.get("company") or {}
                    agency = prices.get("agency") or {}
                    rows.append(
                        {
                            "source": "khodro45",
                            "scraped_at": scraped_at,
                            "pricing_page_url": PRICING_PAGE_URL,
                            "api_url": DAILYCARS_API,
                            "item_slug": item.get("slug"),
                            "trimyear_id": trimyear.get("id"),
                            "trim_id": trim.get("id"),
                            "trim_title": trim.get("title"),
                            "trim_seo_slug": trim.get("seo_slug"),
                            "year_id": year.get("id"),
                            "year_title": year.get("title"),
                            "option_id": option.get("id"),
                            "option_title": option.get("title"),
                            "model_id": nested_model.get("id") or model.get("id"),
                            "model_title": nested_model.get("title") or model.get("title"),
                            "model_seo_slug": nested_model.get("seo_slug") or model.get("seo_slug"),
                            "brand_id": nested_brand.get("id"),
                            "brand_title": nested_brand.get("title") or brand.get("title"),
                            "brand_title_en": brand.get("title_en"),
                            "brand_seo_slug": brand.get("seo_slug"),
                            "market_price": market.get("price"),
                            "market_change_pct": market.get("change_pct"),
                            "market_update_date": market.get("update_date"),
                            "company_price": company.get("price"),
                            "agency_price": agency.get("price"),
                        }
                    )
    return rows


def run_khodro45_pricing_pipeline(
    *,
    output_path: Path,
    dry_run: bool = False,
    limit: int | None = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> Khodro45PricingRunSummary:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    pages: list[Mapping[str, Any]] = []
    errors_http = 0
    try:
        for page in iter_dailycars_pages(session, page_limit=page_limit):
            pages.append(page)
    except requests.RequestException:
        errors_http += 1
        raise

    rows = flatten_dailycar_items(pages)
    if limit is not None:
        rows = rows[: max(0, limit)]

    brands_seen = sum(len(p.get("results") or []) for p in pages)
    items_with_price = sum(1 for r in rows if r.get("market_price") is not None)

    out: str | None = None
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        out = str(output_path)

    return Khodro45PricingRunSummary(
        brands_seen=brands_seen,
        items_seen=len(rows),
        items_with_market_price=items_with_price,
        pages_fetched=len(pages),
        output_path=out,
        errors_http=errors_http,
    )
