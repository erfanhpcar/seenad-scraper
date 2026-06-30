#!/usr/bin/env python3
"""
Live audit for missing Bama zero-km prices (e.g. جک، جی 4 on https://bama.ir/price).

Fetches the public price page HTML (SSR DOM) and the production hierarchy JSON API
(``/cad/api/price/hierarchy`` — same source as ``bama_price_service``). Prints every
extracted brand/model/trim, normalization stages, and mapping results against
``car_entities``.

Usage::

    python scripts/debug_bama_price_missing.py
    python scripts/debug_bama_price_missing.py --filter جک
    python scripts/debug_bama_price_missing.py --max-pages 2 --no-db

Run inside the backend/worker container when DB mapping is required::

    docker compose exec backend python -u /app/scripts/debug_bama_price_missing.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.bama_price_service import (  # noqa: E402
    BAMA_BASE_URL,
    BamaHierarchyItem,
    DEFAULT_PAGE_SIZE,
    USER_AGENT,
    _http_session,
    format_bama_car_display_label,
    format_bama_car_raw_key,
    iterate_hierarchy_items,
)

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def slug_phrase(slug: str) -> str:
    return slug.strip().replace("-", " ").replace("_", " ").strip()


def normalize_compare_key(text: str) -> str:
    return text.translate(_PERSIAN_DIGITS).casefold().strip()


def build_bilingual_query(fa_label: str, slug: str) -> str:
    parts = [fa_label.strip(), slug_phrase(slug)]
    return " ".join(p for p in parts if p).strip()

PRICE_PAGE_URL = f"{BAMA_BASE_URL}/price"

# SSR rows on bama.ir/price (popular / above-the-fold catalogue lines).
DOM_CAR_NAME_SELECTOR = (
    "span.text-base.leading-5.font-semibold.tracking-normal.text-neutral-10"
)
DOM_BRAND_HEADING_SELECTOR = "h3.text-title-large.tracking-normal.font-semibold.text-neutral-10"


@dataclass(frozen=True, slots=True)
class DomCarRow:
    display_label: str
    brand: str
    model: str
    trim: str
    source: str = "dom_ssr"


@dataclass(frozen=True, slots=True)
class NormalizationTrace:
    brand_fa: str
    model_fa: str
    trim_fa: str
    brand_slug: str
    model_slug: str
    trim_slug: str
    brand_compare_key: str
    model_compare_key: str
    trim_compare_key: str
    brand_bilingual_query: str
    model_bilingual_query: str
    trim_bilingual_query: str


def _price_page_headers(*, for_html: bool) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
    }
    if for_html:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Referer"] = f"{BAMA_BASE_URL}/"
    else:
        headers["Accept"] = "application/json, text/plain, */*"
    return headers


def _split_dom_label(raw: str) -> tuple[str, str, str]:
    """Split ``brand , model , trim`` lines rendered on the price page."""

    normalized = re.sub(r"\s*,\s*", ",", raw.strip())
    parts = [p.strip() for p in normalized.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], ", ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 1:
        return parts[0], "", ""
    return "", "", ""


def extract_dom_car_rows(html: str) -> list[DomCarRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[DomCarRow] = []
    seen: set[str] = set()

    for span in soup.select(DOM_CAR_NAME_SELECTOR):
        raw = re.sub(r"\s+", " ", span.get_text(" ", strip=True))
        raw = re.sub(r"\s*,\s*", " , ", raw)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        brand, model, trim = _split_dom_label(raw)
        rows.append(
            DomCarRow(
                display_label=raw.replace(" , ", "، "),
                brand=brand,
                model=model,
                trim=trim,
            )
        )

    for heading in soup.select(DOM_BRAND_HEADING_SELECTOR):
        text = heading.get_text(" ", strip=True)
        if text.startswith("قیمت خودرو "):
            brand_only = text.removeprefix("قیمت خودرو ").strip()
            label = f"{brand_only} (brand heading only)"
            if label not in seen:
                seen.add(label)
                rows.append(
                    DomCarRow(
                        display_label=label,
                        brand=brand_only,
                        model="",
                        trim="",
                        source="dom_brand_heading",
                    )
                )

    return rows


def fetch_price_page_html(session: requests.Session) -> str:
    response = session.get(
        PRICE_PAGE_URL,
        headers=_price_page_headers(for_html=True),
        timeout=45,
    )
    response.raise_for_status()
    return response.text


def fetch_hierarchy_items(
    session: requests.Session,
    *,
    page_size: int,
    max_pages: int | None,
) -> list[tuple[int, BamaHierarchyItem]]:
    items: list[tuple[int, BamaHierarchyItem]] = []
    for page_idx, item in iterate_hierarchy_items(
        session,
        page_size=page_size,
        max_hierarchy_pages=max_pages,
        inter_page_delay_seconds=(0.0, 0.0),
    ):
        items.append((page_idx, item))
    return items


def build_normalization_trace(item: BamaHierarchyItem) -> NormalizationTrace:
    return NormalizationTrace(
        brand_fa=item.brand_fa,
        model_fa=item.model_fa,
        trim_fa=item.trim_fa,
        brand_slug=item.brand_slug,
        model_slug=item.model_slug,
        trim_slug=item.trim_slug,
        brand_compare_key=normalize_compare_key(item.brand_fa),
        model_compare_key=normalize_compare_key(item.model_fa),
        trim_compare_key=normalize_compare_key(item.trim_fa),
        brand_bilingual_query=build_bilingual_query(item.brand_fa, item.brand_slug),
        model_bilingual_query=build_bilingual_query(item.model_fa, item.model_slug),
        trim_bilingual_query=build_bilingual_query(item.trim_fa, item.trim_slug),
    )


def _print_normalization_notes(trace: NormalizationTrace) -> None:
    print(
        "    normalize_compare_key: "
        f"brand={trace.brand_compare_key!r} "
        f"model={trace.model_compare_key!r} "
        f"trim={trace.trim_compare_key!r}"
    )
    print(
        "    slug_phrase: "
        f"brand={slug_phrase(trace.brand_slug)!r} "
        f"model={slug_phrase(trace.model_slug)!r} "
        f"trim={slug_phrase(trace.trim_slug)!r}"
    )
    print(
        "    bilingual_query: "
        f"brand={trace.brand_bilingual_query!r} "
        f"model={trace.model_bilingual_query!r} "
        f"trim={trace.trim_bilingual_query!r}"
    )
    print(
        "    note: normalize_compare_key keeps Persian commas/spaces; "
        "only Persian digits → ASCII and casefold are applied."
    )
    if not trace.trim_fa.strip() and not trace.trim_slug.strip():
        print("    ⚠ empty trim_fa/trim_slug — trim fuzzy match often fails (see JAC J4).")


def _matches_filter(text: str, needle: str) -> bool:
    if not needle:
        return True
    folded = needle.casefold()
    return folded in text.casefold() or folded in normalize_compare_key(text)


def _item_matches_filter(item: BamaHierarchyItem, needle: str) -> bool:
    if not needle:
        return True
    blob = " ".join(
        (
            item.brand_fa,
            item.model_fa,
            item.trim_fa,
            item.brand_slug,
            item.model_slug,
            item.trim_slug,
            format_bama_car_display_label(item),
        )
    )
    return _matches_filter(blob, needle)


def _configure_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filter",
        default="",
        help="Only print rows whose label contains this substring (e.g. جک or J4).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Hierarchy API page size (default {DEFAULT_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap hierarchy pages (default: full catalogue).",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip car_entities mapping (API + normalization only).",
    )
    return parser.parse_args()


def main() -> int:
    args = _configure_args()
    filter_needle = args.filter.strip()

    session = _http_session()

    print("=== 1) Fetch bama.ir/price HTML (SSR DOM) ===")
    print(f"URL: {PRICE_PAGE_URL}")
    print(f"DOM car-name selector: {DOM_CAR_NAME_SELECTOR}")
    html = fetch_price_page_html(session)
    dom_rows = extract_dom_car_rows(html)
    if filter_needle:
        dom_rows = [
            row
            for row in dom_rows
            if _matches_filter(row.display_label, filter_needle)
            or _matches_filter(row.brand, filter_needle)
        ]
    print(f"DOM rows extracted: {len(dom_rows)}")
    for row in dom_rows:
        print(
            f"  [DOM/{row.source}] display={row.display_label!r} "
            f"brand={row.brand!r} model={row.model!r} trim={row.trim!r}"
        )
    if not dom_rows:
        print(
            "  (no matching DOM rows — price catalogue is mostly client-rendered; "
            "use hierarchy API below for the full list.)"
        )

    print("\n=== 2) Fetch production hierarchy API ===")
    api_items = fetch_hierarchy_items(
        session,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    if filter_needle:
        api_items = [(p, item) for p, item in api_items if _item_matches_filter(item, filter_needle)]
    print(f"API hierarchy rows: {len(api_items)}")
    for page_idx, item in api_items:
        label = format_bama_car_display_label(item)
        print(
            f"  [API p{page_idx}] {label!r} | "
            f"brand_fa={item.brand_fa!r} model_fa={item.model_fa!r} trim_fa={item.trim_fa!r} | "
            f"slugs={item.brand_slug}/{item.model_slug}/{item.trim_slug or '-'} | "
            f"price_toman={item.price_api_raw} car_price_id={item.bama_car_price_id}"
        )
        trace = build_normalization_trace(item)
        _print_normalization_notes(trace)

    print("\n=== 3) DB mapping (disabled on Scraper Server) ===")
    if not args.no_db:
        print(
            "car_entities was removed in Phase C; mapping runs on the Main Server. "
            "Use --no-db (default behaviour) or inspect car_zero_price_scrape_snapshot batches."
        )

    priced_rows = sum(1 for _p, item in api_items if item.price_api_raw > 0)

    print("\n=== Summary ===")
    print(f"DOM SSR rows (visible on /price HTML): {len(dom_rows)}")
    print(f"API hierarchy rows (full catalogue):   {len(api_items)}")
    print(f"API rows with price > 0:               {priced_rows}")
    print("Entity mapping: disabled (Phase C scraper DB cleanup).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
