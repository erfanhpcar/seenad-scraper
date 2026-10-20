#!/usr/bin/env python3
"""Probe Khodro45 /pricing/ daily cars API (sample rows + counts)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.khodro45_pricing_service import (
    DAILYCARS_API,
    PRICING_PAGE_URL,
    flatten_dailycar_items,
    iter_dailycars_pages,
)

OUTPUT = Path("data/khodro45_pricing_probe_sample.json")


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    pages = list(iter_dailycars_pages(session, page_limit=50))
    rows = flatten_dailycar_items(pages)
    sample = {
        "pricing_page": PRICING_PAGE_URL,
        "api": DAILYCARS_API,
        "pages_fetched": len(pages),
        "api_count": pages[0].get("count") if pages else None,
        "items_flattened": len(rows),
        "items_with_market_price": sum(1 for r in rows if r.get("market_price") is not None),
        "sample_rows": rows[:5],
        "meta": pages[0].get("meta") if pages else None,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
