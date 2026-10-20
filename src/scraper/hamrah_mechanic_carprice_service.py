"""
Hamrah Mechanic zero / market prices from https://www.hamrah-mechanic.com/carprice/

The public Next.js pages embed the full per-brand price table in ``__NEXT_DATA__``:
  /carprice/              → ``brandPricesList`` (subset on home)
  /carprice/{brand_en}/   → ``modelPriceList.carPriceByBrandDetails`` (full brand catalogue)

We crawl every brand slug from the ``brands`` nav list (typically ~93 brands).
Run inside the scraper Docker stack (``extra_hosts`` for hamrah-mechanic.com).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import requests

LOGGER: Final = logging.getLogger(__name__)

CARPRICE_HOME: Final[str] = "https://www.hamrah-mechanic.com/carprice/"
BRAND_LIST_SEED_URL: Final[str] = "https://www.hamrah-mechanic.com/carprice/audi/"
NEXT_DATA_RE: Final[re.Pattern[str]] = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 45.0
DEFAULT_HTTP_MAX_ATTEMPTS: Final[int] = 5
DEFAULT_INTER_REQUEST_SLEEP_S: Final[float] = 0.03
RETRYABLE_HTTP_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class HamrahCarpriceRunSummary:
    brands_total: int
    brands_ok: int
    brands_failed: int
    items_seen: int
    items_unique: int
    output_path: str | None
    failed_brands: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "hamrah-mechanic",
            "carprice_home": CARPRICE_HOME,
            "brands_total": self.brands_total,
            "brands_ok": self.brands_ok,
            "brands_failed": self.brands_failed,
            "items_seen": self.items_seen,
            "items_unique": self.items_unique,
            "output_path": self.output_path,
            "failed_brands": list(self.failed_brands),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get_text(session: requests.Session, url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, DEFAULT_HTTP_MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=DEFAULT_REQUEST_TIMEOUT_S)
            if response.status_code in RETRYABLE_HTTP_STATUS:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= DEFAULT_HTTP_MAX_ATTEMPTS:
                break
            delay = min(2.0 ** attempt + random.random(), 30.0)
            LOGGER.warning("hamrah carprice GET retry %s/%s url=%s err=%s", attempt, DEFAULT_HTTP_MAX_ATTEMPTS, url, exc)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def parse_next_page_props(html: str) -> dict[str, Any]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("__NEXT_DATA__ not found")
    payload = json.loads(match.group(1))
    page_props = (payload.get("props") or {}).get("pageProps")
    if not isinstance(page_props, dict):
        raise ValueError("pageProps missing")
    return page_props


def fetch_brand_slugs(session: requests.Session) -> list[dict[str, Any]]:
    html = _http_get_text(session, BRAND_LIST_SEED_URL)
    props = parse_next_page_props(html)
    brands = props.get("brands")
    if not isinstance(brands, list):
        raise ValueError("brands list missing on seed page")
    return brands


def _brand_slug_candidates(slug: str) -> list[str]:
    """HM sometimes 301-loops on UTF-8 slugs (e.g. škoda); try ASCII fallbacks."""
    slug = slug.strip()
    if not slug:
        return []
    lower = slug.lower()
    if lower in {"škoda", "skoda"} or "škoda" in lower:
        return ["skoda", slug, lower]
    candidates = [slug]
    if lower not in candidates:
        candidates.append(lower)
    return candidates


def _fetch_brand_carprice_html(session: requests.Session, slug: str) -> tuple[str, str]:
    last_exc: Exception | None = None
    for cand in _brand_slug_candidates(slug):
        page_url = f"https://www.hamrah-mechanic.com/carprice/{quote(cand, safe='')}/"
        try:
            response = session.get(page_url, timeout=DEFAULT_REQUEST_TIMEOUT_S, allow_redirects=True)
            response.raise_for_status()
            return response.text, page_url
        except requests.RequestException as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def flatten_brand_details(
    *,
    brand_meta: dict[str, Any],
    page_props: dict[str, Any],
    scraped_at: str,
    page_url: str,
) -> list[dict[str, Any]]:
    model_price_list = page_props.get("modelPriceList") or {}
    details = model_price_list.get("carPriceByBrandDetails") or []
    brand_name = model_price_list.get("brandName") or brand_meta.get("brandName")
    last_update = model_price_list.get("lastUpdateTime")
    rows: list[dict[str, Any]] = []
    for detail in details:
        rows.append(
            {
                "source": "hamrah-mechanic",
                "scraped_at": scraped_at,
                "carprice_home_url": CARPRICE_HOME,
                "page_url": page_url,
                "brand_id": brand_meta.get("brandId"),
                "brand_name": brand_name,
                "brand_english": brand_meta.get("brandEnglishName"),
                "model_name": detail.get("modelName"),
                "model_english": detail.get("modelEnglishName"),
                "car_model_id": detail.get("carModelId"),
                "car_type_name": detail.get("carTypeName"),
                "car_type_id": detail.get("carTypeId"),
                "car_year": detail.get("carYear"),
                "market_price": detail.get("marketPrice"),
                "brand_last_update": last_update,
            }
        )
    return rows


def run_hamrah_carprice_pipeline(
    *,
    output_path: Path,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_s: float = DEFAULT_INTER_REQUEST_SLEEP_S,
) -> HamrahCarpriceRunSummary:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})

    brands = fetch_brand_slugs(session)
    scraped_at = _utc_now_iso()
    all_rows: list[dict[str, Any]] = []
    failed: list[str] = []
    ok = 0

    for brand in brands:
        slug = (brand.get("brandEnglishName") or "").strip()
        if not slug:
            failed.append("<missing-slug>")
            continue
        try:
            html, page_url = _fetch_brand_carprice_html(session, slug)
            props = parse_next_page_props(html)
            all_rows.extend(
                flatten_brand_details(
                    brand_meta=brand,
                    page_props=props,
                    scraped_at=scraped_at,
                    page_url=page_url,
                )
            )
            ok += 1
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("hamrah brand crawl failed slug=%s err=%s", slug, exc)
            failed.append(slug)
        if sleep_s > 0:
            time.sleep(sleep_s)
        if limit is not None and len(all_rows) >= limit:
            all_rows = all_rows[:limit]
            break

    unique_keys = {(r.get("car_type_id"), r.get("car_year")) for r in all_rows}

    out: str | None = None
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for row in all_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        out = str(output_path)

    return HamrahCarpriceRunSummary(
        brands_total=len(brands),
        brands_ok=ok,
        brands_failed=len(failed),
        items_seen=len(all_rows),
        items_unique=len(unique_keys),
        output_path=out,
        failed_brands=tuple(failed),
    )
