"""
Temporary probe: Behtarino.com automotive directory (car dealers / repair shops).

Inspects listing URL structure, anti-bot behavior, SSR/hydration, pagination,
and whether phones/coordinates appear in the initial HTML vs detail pages.

Usage:
  python scripts/temp_behtarino_probe.py
  python scripts/temp_behtarino_probe.py --category نمایشگاه-خودرو --city تهران
  python scripts/temp_behtarino_probe.py --category تعمیرگاه-خودرو --city تهران --limit 3

Dependencies:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://behtarino.com"
DEFAULT_CATEGORY = "نمایشگاه-خودرو"
DEFAULT_CITY = "تهران"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OUTPUT_PATH = Path("data/behtarino_probe_sample.json")

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "access denied",
)
CANDIDATE_LISTING_PATHS = (
    "/s/car-dealership",
    "/s/car-dealership/tehran",
    f"/r/{DEFAULT_CATEGORY}",
    f"/r/{DEFAULT_CATEGORY}/{DEFAULT_CITY}",
)


def build_listing_url(category: str, city: str | None) -> str:
    path = f"/r/{category}"
    if city:
        path = f"{path}/{city}"
    return BASE_URL + quote(path, safe="/:")


def fetch_html(session: requests.Session, url: str, *, timeout: float = 30) -> tuple[int, str, dict[str, str]]:
    response = session.get(url, timeout=timeout)
    # Behtarino serves UTF-8; apparent_encoding can mis-detect and mangle Persian JSON-LD.
    response.encoding = "utf-8"
    headers = {k.lower(): v for k, v in response.headers.items()}
    return response.status_code, response.text, headers


def detect_anti_bot(status: int, html: str, headers: dict[str, str]) -> dict[str, Any]:
    lowered = html.lower()
    return {
        "http_status": status,
        "blocked_likely": status in {403, 429, 503} or any(marker in lowered for marker in ANTI_BOT_MARKERS),
        "cloudflare_signals": "cf-ray" in headers or "cloudflare" in lowered[:8000],
        "arvan_signals": "arvan" in str(headers).lower() or "arvancloud" in lowered[:8000],
        "cdn_server": headers.get("server"),
        "x_powered_by": headers.get("x-powered-by"),
        "challenge_page_detected": any(marker in lowered for marker in ANTI_BOT_MARKERS),
    }


def detect_rendering_framework(html: str) -> dict[str, Any]:
    return {
        "framework": "nextjs_app_router_rsc" if "self.__next_f.push" in html else "unknown",
        "has___NEXT_DATA__": "__NEXT_DATA__" in html,
        "has_self___next_f": "self.__next_f.push" in html,
        "has_window___NUXT__": "window.__NUXT__=" in html,
        "has_nuxt_payload": "__NUXT__" in html and "window.__NUXT__" not in html,
        "next_static_assets": "/_next/static/" in html,
    }


def extract_search_results_json_ld(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    businesses: list[dict[str, Any]] = []

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        if not isinstance(payload, dict) or payload.get("@type") != "SearchResultsPage":
            continue

        main_entity = payload.get("mainEntity")
        if not isinstance(main_entity, dict):
            continue

        for element in main_entity.get("itemListElement", []):
            if not isinstance(element, dict):
                continue
            item = element.get("item")
            if not isinstance(item, dict):
                continue

            geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
            address = item.get("address") if isinstance(item.get("address"), dict) else {}
            businesses.append(
                {
                    "position": element.get("position"),
                    "name": item.get("name"),
                    "profile_url": item.get("url"),
                    "latitude": geo.get("latitude"),
                    "longitude": geo.get("longitude"),
                    "street_address": address.get("streetAddress"),
                    "address_locality": address.get("addressLocality"),
                    "address_region": address.get("addressRegion"),
                    "telephone": item.get("telephone"),
                    "source": "json_ld_search_results",
                }
            )

    return businesses


def parse_listing_cards_dom(html: str, *, base_url: str, limit: int | None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []

    for anchor in soup.select('a[href^="/p/"]'):
        href = anchor.get("href", "")
        profile_url = urljoin(base_url, href) if href else None
        slug = href.removeprefix("/p/").split("~", 1)[0] if href.startswith("/p/") else None

        card_root = anchor
        for _ in range(8):
            parent = card_root.parent
            if parent is None:
                break
            if len(parent.get_text(strip=True)) > len(card_root.get_text(strip=True)):
                card_root = parent
            else:
                break

        cards.append(
            {
                "slug": slug,
                "title": anchor.get_text(" ", strip=True) or None,
                "profile_url": profile_url,
                "primary_phone": None,
                "source": "ssr_dom",
            }
        )
        if limit is not None and len(cards) >= limit:
            break

    return cards


def analyze_pagination(session: requests.Session, listing_url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    page_links: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        if "page=" not in anchor["href"]:
            continue
        match = re.search(r"page=(\d+)", anchor["href"])
        page_links.append(
            {
                "href": urljoin(listing_url, anchor["href"]),
                "page": int(match.group(1)) if match else None,
                "label": anchor.get_text(strip=True),
            }
        )

    max_linked_page = max((link["page"] for link in page_links if link["page"] is not None), default=1)

    page1_slugs = _profile_slugs_from_html(html)
    status_p2, html_p2, _ = fetch_html(session, f"{listing_url}?page=2")
    page2_slugs = _profile_slugs_from_html(html_p2) if status_p2 == 200 else []

    return {
        "mechanism": "query_param_page",
        "pagination_links_sample": page_links[:8],
        "max_page_in_nav": max_linked_page,
        "page_1_profile_count": len(page1_slugs),
        "page_2_http_status": status_p2,
        "page_2_profile_count": len(page2_slugs),
        "page_1_and_2_overlap": len(set(page1_slugs) & set(page2_slugs)),
        "recommended_pattern": f"{listing_url}?page=<1-based>",
    }


def _profile_slugs_from_html(html: str) -> list[str]:
    slugs: list[str] = []
    for href in re.findall(r'href="(/p/[^"]+)"', html):
        slug = href.removeprefix("/p/").split("~", 1)[0]
        slugs.append(slug)
    return slugs


def scan_initial_payload_signals(html: str) -> dict[str, Any]:
    geo_pairs = re.findall(
        r'\{"@type":"GeoCoordinates","latitude":([-\d.]+),"longitude":([-\d.]+)\}',
        html,
    )
    tel_links = re.findall(r'href="tel:([^"]+)"', html)
    phone_literals = re.findall(r'"telephone"\s*:\s*"([^"]+)"', html)
    mobile_patterns = re.findall(r"\b09\d{9}\b", html)

    return {
        "geo_coordinates_in_html": len(geo_pairs),
        "geo_sample": [{"latitude": float(lat), "longitude": float(lng)} for lat, lng in geo_pairs[:3]],
        "tel_links_in_dom": tel_links[:5],
        "telephone_in_json_ld": phone_literals[:5],
        "mobile_pattern_hits": len(set(mobile_patterns)),
        "coordinates_in_initial_payload": len(geo_pairs) > 0,
        "phones_in_listing_initial_payload": bool(tel_links or phone_literals),
    }


def hunt_api_endpoints(session: requests.Session, category: str, city: str | None) -> list[dict[str, Any]]:
    candidates: list[tuple[str, dict[str, Any] | None]] = [
        (f"{BASE_URL}/api/v1/business/search", {"category": category, "city": city or ""}),
        (f"{BASE_URL}/api/business/search", {"category": category, "city": city or ""}),
        (f"{BASE_URL}/api/v1/rankpage/search", {"tag": category, "city": city or ""}),
    ]

    results: list[dict[str, Any]] = []
    for url, params in candidates:
        try:
            response = session.get(
                url,
                params=params,
                timeout=12,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            results.append({"url": url, "error": str(exc)})
            continue

        content_type = response.headers.get("content-type", "")
        entry: dict[str, Any] = {
            "url": url,
            "http_status": response.status_code,
            "content_type": content_type,
            "looks_like_json_api": "application/json" in content_type and response.status_code == 200,
        }
        if "application/json" in content_type:
            try:
                entry["json_keys"] = list(response.json().keys())[:12]
            except ValueError:
                entry["body_preview"] = response.text[:200]
        else:
            entry["body_preview"] = response.text[:120].replace("\n", " ")

        results.append(entry)

    return results


def probe_detail(session: requests.Session, profile_url: str) -> dict[str, Any]:
    status, html, _ = fetch_html(session, profile_url)
    if status != 200:
        return {"profile_url": profile_url, "http_status": status, "error": "detail fetch failed"}

    soup = BeautifulSoup(html, "html.parser")
    tel_links = [
        anchor["href"].replace("tel:", "").strip()
        for anchor in soup.select('a[href^="tel:"]')
    ]
    mobiles = sorted(set(re.findall(r"\b09\d{9}\b", html)))
    landlines = sorted(set(re.findall(r"\b0[1-8]\d{9}\b", html)))

    geo = re.findall(
        r'\{"@type":"GeoCoordinates","latitude":([-\d.]+),"longitude":([-\d.]+)\}',
        html,
    )

    return {
        "profile_url": profile_url,
        "http_status": status,
        "tel_links": tel_links,
        "mobile_numbers_found": mobiles[:5],
        "landline_numbers_found": landlines[:5],
        "geo_in_detail_html": [{"latitude": float(a), "longitude": float(b)} for a, b in geo[:2]],
        "phones_on_detail_page": bool(tel_links or mobiles or landlines),
    }


def resolve_listing_url(
    session: requests.Session, category: str, city: str | None
) -> tuple[str, int, str, dict[str, str]]:
    primary = build_listing_url(category, city)
    status, html, headers = fetch_html(session, primary)
    if status == 200 and 'href="/p/' in html:
        return primary, status, html, headers

    for path in CANDIDATE_LISTING_PATHS:
        url = BASE_URL + quote(path, safe="/:")
        status, html, headers = fetch_html(session, url)
        if status == 200 and 'href="/p/' in html:
            return url, status, html, headers

    return primary, status, html, headers


def recommend_strategy(
    anti_bot: dict[str, Any],
    rendering: dict[str, Any],
    payload_signals: dict[str, Any],
    json_ld_rows: list[dict[str, Any]],
    api_hunt: list[dict[str, Any]],
) -> str:
    if anti_bot.get("blocked_likely"):
        return "playwright_or_residential_proxy — programmatic requests appear blocked"

    public_api = any(row.get("looks_like_json_api") for row in api_hunt)
    if public_api:
        return "internal_json_api — use discovered REST endpoint for pagination"

    if json_ld_rows and payload_signals.get("coordinates_in_initial_payload"):
        phones_on_listing = payload_signals.get("phones_in_listing_initial_payload")
        if phones_on_listing:
            return "html_json_ld_parsing — listing SSR embeds JSON-LD ItemList with geo and phone"
        return (
            "html_json_ld_pagination_plus_detail_fetch — parse SearchResultsPage JSON-LD per "
            "?page=N for names/geo/urls; fetch /p/{slug} detail pages for phone numbers"
        )

    if rendering.get("has_self___next_f"):
        return "html_dom_or_rsc_decode — Next.js RSC payload; prefer JSON-LD if present else DOM anchors"

    return "html_dom_parsing — fall back to visible card anchors"


def build_sample_artifact(report: dict[str, Any]) -> dict[str, Any]:
    listing = report.get("listing_sample", {})
    json_ld = listing.get("from_json_ld", [])
    dom = listing.get("from_ssr_dom", [])
    return {
        "listing_url": report.get("listing_url"),
        "recommended_strategy": report.get("recommended_strategy"),
        "json_ld_listing_sample": json_ld[:2],
        "dom_listing_sample": dom[:2],
        "pagination": report.get("pagination"),
        "detail_probe": report.get("detail_probe"),
        "api_hunt": report.get("api_hunt"),
    }


def print_diagnostic_report(report: dict[str, Any]) -> None:
    anti_bot = report["anti_bot"]
    rendering = report["rendering"]
    payload = report["payload_signals"]
    pagination = report["pagination"]

    print("=" * 72)
    print("BEHTARINO PROBE — DIAGNOSTIC REPORT")
    print("=" * 72)
    print(f"Listing URL .............. {report['listing_url']}")
    print(f"HTTP status .............. {anti_bot['http_status']}")
    print(f"Anti-bot blocked ......... {anti_bot['blocked_likely']}")
    print(f"CDN / server ............. {anti_bot.get('cdn_server')} (x-powered-by: {anti_bot.get('x_powered_by')})")
    print(f"Cloudflare signals ....... {anti_bot['cloudflare_signals']}")
    print(f"Arvan signals ............ {anti_bot['arvan_signals']}")
    print()
    print(f"Rendering framework ...... {rendering['framework']}")
    print(f"__NEXT_DATA__ ............ {rendering['has___NEXT_DATA__']}")
    print(f"self.__next_f (RSC) ...... {rendering['has_self___next_f']}")
    print()
    print(f"Cards (JSON-LD) .......... {len(report['listing_sample']['from_json_ld'])}")
    print(f"Cards (DOM /p/ links) .... {len(report['listing_sample']['from_ssr_dom'])}")
    print(f"Coords in initial HTML ... {payload['coordinates_in_initial_payload']} ({payload['geo_coordinates_in_html']} geo tags)")
    print(f"Phones in listing HTML ... {payload['phones_in_listing_initial_payload']}")
    print()
    print(f"Pagination ............... {pagination['mechanism']}")
    print(f"Max page in nav .......... {pagination['max_page_in_nav']}")
    print(f"Page 2 overlap ........... {pagination['page_1_and_2_overlap']} slugs")
    print()
    print("API hunt (public REST):")
    for row in report["api_hunt"]:
        if row.get("looks_like_json_api"):
            print(f"  ✓ {row['url']} -> JSON API")
        else:
            print(f"  ✗ {row.get('url')} status={row.get('http_status')} ({row.get('content_type', row.get('error', ''))[:40]})")
    print()
    print(f"RECOMMENDED STRATEGY: {report['recommended_strategy']}")
    print("=" * 72)
    print(f"Sample saved to: {report['sample_output_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Behtarino.com automotive listing probe")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help="Category slug under /r/")
    parser.add_argument("--city", default=DEFAULT_CITY, help="City slug (omit with --no-city)")
    parser.add_argument("--no-city", action="store_true", help="Use Iran-wide /r/{category} without city")
    parser.add_argument("--limit", type=int, default=5, help="Sample cards to include in output")
    parser.add_argument("--skip-detail", action="store_true", help="Skip first business detail probe")
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="Path for JSON sample artifact",
    )
    args = parser.parse_args()

    city = None if args.no_city else args.city
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    listing_url, status, html, response_headers = resolve_listing_url(session, args.category, city)
    anti_bot = detect_anti_bot(status, html, response_headers)

    if anti_bot["blocked_likely"] or status != 200:
        report = {
            "blocked": True,
            "listing_url": listing_url,
            "anti_bot": anti_bot,
            "message": "Listing fetch blocked or non-200; try Playwright or residential proxy.",
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    rendering = detect_rendering_framework(html)
    json_ld_rows = extract_search_results_json_ld(html)
    dom_rows = parse_listing_cards_dom(html, base_url=listing_url, limit=args.limit)
    if args.limit is not None:
        json_ld_rows = json_ld_rows[: args.limit]

    payload_signals = scan_initial_payload_signals(html)
    pagination = analyze_pagination(session, listing_url, html)
    api_hunt = hunt_api_endpoints(session, args.category, city)

    detail_probe: dict[str, Any] | None = None
    if not args.skip_detail:
        profile_url = None
        if json_ld_rows and json_ld_rows[0].get("profile_url"):
            profile_url = json_ld_rows[0]["profile_url"]
        elif dom_rows and dom_rows[0].get("profile_url"):
            profile_url = dom_rows[0]["profile_url"]
        if profile_url:
            detail_probe = probe_detail(session, profile_url)

    recommended = recommend_strategy(anti_bot, rendering, payload_signals, json_ld_rows, api_hunt)

    report: dict[str, Any] = {
        "blocked": False,
        "listing_url": listing_url,
        "category_slug": args.category,
        "city_slug": city,
        "url_pattern_note": "Behtarino uses /r/{category-slug}/{city-slug} (Persian slugs), not /s/car-dealership",
        "anti_bot": anti_bot,
        "rendering": rendering,
        "payload_signals": payload_signals,
        "pagination": pagination,
        "api_hunt": api_hunt,
        "listing_sample": {
            "from_json_ld": json_ld_rows,
            "from_ssr_dom": dom_rows,
        },
        "detail_probe": detail_probe,
        "recommended_strategy": recommended,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sample = build_sample_artifact(report)
    output_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    report["sample_output_path"] = str(output_path)

    print_diagnostic_report(report)
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
