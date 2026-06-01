"""
Temporary probe: Torob search for car parts (raw-ish extraction).

Run on a machine/IP that Torob does not challenge with Arcaptcha (often fails from
datacenters). Prefer: residential IP, or Playwright with a saved storage_state after
manual login/captcha once.

Usage:
  python scripts/temp_torob_parts_probe.py
  python scripts/temp_torob_parts_probe.py --query "لنت ترمز ۲۰۷" --limit 10 --fetch-shop

Dependencies:
  pip install requests
  # optional second mode:
  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping
from urllib.parse import quote_plus, urljoin

import requests

TOROB_ORIGIN = "https://torob.com"
SEARCH_API = "https://api.torob.com/v4/base-product/search/"


def _first_str(*candidates: object) -> str | None:
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            return str(c)
    return None


def _build_search_url(query: str, *, page: int = 0, size: int = 24) -> str:
    """Mirror public clients (e.g. torobot): source=next_desktop + popularity sort."""
    q = quote_plus(query)
    return (
        f"{SEARCH_API}?page={page}"
        f"&sort=popularity&size={size}"
        f"&query={q}&available=true&q={q}&source=next_desktop"
    )


def _product_page_url(item: Mapping[str, Any]) -> str | None:
    path = item.get("web_client_absolute_url")
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return urljoin(TOROB_ORIGIN, path)


def _maybe_shop_from_more_info(
    session: requests.Session,
    item: Mapping[str, Any],
    *,
    timeout: float,
) -> str | None:
    """
    Search cards are often aggregated; `shop_text` may be counts.
    `more_info_url` (absolute API URL) lists per-shop rows with `shop_name`.
    """
    url = item.get("more_info_url")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    try:
        detail = session.get(url, timeout=timeout).json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return None
    pinfo = detail.get("products_info")
    if not isinstance(pinfo, dict):
        return None
    results = pinfo.get("result")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    return _first_str(first.get("shop_name"), first.get("shop_name2"))


def fetch_via_api(
    query: str,
    *,
    limit: int,
    timeout: float,
    fetch_shop: bool,
) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": TOROB_ORIGIN,
            "Referer": f"{TOROB_ORIGIN}/",
        }
    )

    out: list[dict[str, Any]] = []
    next_url: str | None = _build_search_url(query, page=0, size=24)

    while next_url and len(out) < limit:
        resp = session.get(next_url, timeout=timeout)
        if "application/json" not in resp.headers.get("Content-Type", "").lower():
            snippet = resp.text[:400].replace("\n", " ")
            raise RuntimeError(
                "Torob returned non-JSON (likely bot/captcha page). "
                f"HTTP {resp.status_code}. Snippet: {snippet!r}"
            )
        payload = resp.json()
        results = payload.get("results")
        if not isinstance(results, list):
            break

        for item in results:
            if len(out) >= limit:
                break
            if not isinstance(item, dict):
                continue

            title = _first_str(item.get("name1"), item.get("name2")) or ""
            price_val = item.get("price")
            price_text = _first_str(item.get("price_text"))
            if price_text is None and price_val is not None:
                price_text = str(price_val)

            stock = _first_str(item.get("stock_status"))
            page_url = _product_page_url(item)

            shop_guess = _first_str(
                item.get("shop_name"),
                item.get("shop_second_name"),
                item.get("shop_text"),
            )
            if fetch_shop and shop_guess is None:
                shop_guess = _maybe_shop_from_more_info(session, item, timeout=timeout)

            out.append(
                {
                    "title": title,
                    "price": price_text,
                    "shop_name": shop_guess or "",
                    "stock_status": stock or "",
                    "product_url": page_url or "",
                }
            )

        next_raw = payload.get("next")
        next_url = next_raw if isinstance(next_raw, str) and next_raw.startswith("http") else None

    return out


def fetch_via_playwright(query: str, *, limit: int, headless: bool) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Playwright not installed. pip install playwright && playwright install chromium") from exc

    captured: list[dict[str, Any]] = []

    def _on_response(response: Any) -> None:
        try:
            url = response.url
            if "base-product/search" not in url:
                return
            if response.status != 200:
                return
            ctype = response.headers.get("content-type", "")
            if "application/json" not in ctype.lower():
                return
            payload = response.json()
        except Exception:
            return
        results = payload.get("results")
        if not isinstance(results, list):
            return
        for item in results[:limit]:
            if isinstance(item, dict):
                captured.append(item)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="fa-IR")
        page = context.new_page()
        page.on("response", _on_response)
        page.goto(
            f"{TOROB_ORIGIN}/browse/?q={quote_plus(query)}",
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        page.wait_for_timeout(8000)
        browser.close()

    if not captured:
        raise RuntimeError(
            "Playwright did not capture search JSON. Page may show captcha or selectors changed."
        )

    merged: dict[str, dict[str, Any]] = {}
    for row in captured:
        key = str(row.get("random_key") or row.get("more_info_url") or id(row))
        merged[key] = row
    items = list(merged.values())[:limit]

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

    out: list[dict[str, Any]] = []
    for item in items:
        title = _first_str(item.get("name1"), item.get("name2")) or ""
        price_val = item.get("price")
        price_text = _first_str(item.get("price_text"))
        if price_text is None and price_val is not None:
            price_text = str(price_val)
        stock = _first_str(item.get("stock_status"))
        page_url = _product_page_url(item) or ""
        shop = _first_str(item.get("shop_name"), item.get("shop_text")) or _maybe_shop_from_more_info(
            session, item, timeout=20.0
        )
        out.append(
            {
                "title": title,
                "price": price_text or "",
                "shop_name": shop or "",
                "stock_status": stock or "",
                "product_url": page_url,
            }
        )
    return out


def _pattern_hints(titles: list[str]) -> dict[str, Any]:
    """Lightweight textual stats for future regex work (no ML)."""
    brandish = (
        "ایساکو",
        "تکستار",
        "الیگ",
        "لنت پارس",
        "های کیو",
        "بوش",
        "مزدا",
        "پژو",
        "ایران خودرو",
        "سایپا",
        "معتبر",
        "شرکتی",
        "اصلی",
    )
    position_words = ("جلو", "عقب", "چرخ جلو", "چرخ عقب", "جلویی", "عقبی")

    def count_word(w: str) -> int:
        return sum(1 for t in titles if w in t)

    return {
        "brand_or_oem_tokens_found": {b: count_word(b) for b in brandish if count_word(b)},
        "position_tokens_found": {w: count_word(w) for w in position_words if count_word(w)},
        "notes": (
            "Brand names usually appear inside `name1` (Persian title): "
            "often after the part name or in parentheses; English brand may appear in `name2`."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Torob search probe for raw parts rows.")
    parser.add_argument("--query", default="لنت ترمز ۲۰۷")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--method", choices=("api", "playwright"), default="api")
    parser.add_argument("--fetch-shop", action="store_true", help="Call more_info_url for shop_name when missing.")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--playwright-headed", action="store_true", help="Use headed Chromium (helps with captcha).")
    args = parser.parse_args()

    try:
        if args.method == "api":
            rows = fetch_via_api(
                args.query,
                limit=args.limit,
                timeout=args.timeout,
                fetch_shop=args.fetch_shop,
            )
        else:
            rows = fetch_via_playwright(args.query, limit=args.limit, headless=not args.playwright-headed)
    except Exception as exc:  # noqa: BLE001 - probe script
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(rows, ensure_ascii=False, indent=2))
    titles = [r.get("title", "") for r in rows if isinstance(r.get("title"), str)]
    hints = _pattern_hints(titles)
    print("\n--- pattern_hints ---", file=sys.stderr)
    print(json.dumps(hints, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
