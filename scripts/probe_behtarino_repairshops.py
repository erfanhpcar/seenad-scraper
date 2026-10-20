#!/usr/bin/env python3
"""
Deep probe: Behtarino.com car repair shops (تعمیرگاه-خودرو).

Maps every extractable field from listing pages and business detail pages
before writing a production crawler.

Usage (Docker):
  docker compose exec -T backend python3 scripts/probe_behtarino_repairshops.py
  docker compose exec -T backend python3 scripts/probe_behtarino_repairshops.py --city اصفهان --limit 3 -v
  docker compose exec -T backend python3 scripts/probe_behtarino_repairshops.py --no-city --limit 5

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://behtarino.com"
DEFAULT_CATEGORY = "تعمیرگاه-خودرو"
DEFAULT_CITY = "تهران"
OUTPUT_PATH = Path("data/behtarino_repairshops_probe_sample.json")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
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


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def build_listing_url(category: str, city: str | None) -> str:
    path = f"/r/{category}"
    if city:
        path = f"{path}/{city}"
    return BASE_URL + quote(path, safe="/:")


def fetch_html(
    session: requests.Session, url: str, *, timeout: float = 30.0
) -> tuple[int, str, dict[str, str]]:
    response = session.get(url, timeout=timeout)
    response.encoding = "utf-8"
    headers = {k.lower(): v for k, v in response.headers.items()}
    return response.status_code, response.text, headers


def detect_anti_bot(status: int, html: str, headers: dict[str, str]) -> dict[str, Any]:
    lowered = html.lower()
    return {
        "http_status": status,
        "blocked_likely": status in {403, 429, 503}
        or any(marker in lowered for marker in ANTI_BOT_MARKERS),
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
        "next_static_assets": "/_next/static/" in html,
        "html_bytes": len(html.encode("utf-8")),
    }


def _collect_keys(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 4) -> set[str]:
    keys: set[str] = set()
    if depth > max_depth:
        return keys
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.add(path)
            keys |= _collect_keys(value, path, depth + 1, max_depth)
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:3]):
            keys |= _collect_keys(value, f"{prefix}[]", depth + 1, max_depth)
            if index >= 0:
                break
    return keys


def extract_all_json_ld(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError as exc:
            blocks.append({"parse_error": str(exc), "raw_preview": script.string[:200]})
            continue
        blocks.append(payload if isinstance(payload, dict) else {"_list": payload})
    return blocks


def summarize_json_ld_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    types: list[str] = []
    key_union: set[str] = set()
    for block in blocks:
        if "@type" in block:
            types.append(str(block["@type"]))
        key_union |= _collect_keys(block)
    return {
        "block_count": len(blocks),
        "types": types,
        "key_paths": sorted(key_union),
    }


def extract_search_results(html: str) -> list[dict[str, Any]]:
    businesses: list[dict[str, Any]] = []
    for block in extract_all_json_ld(html):
        if block.get("@type") != "SearchResultsPage":
            continue
        main_entity = block.get("mainEntity")
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
                    "postal_code": address.get("postalCode"),
                    "telephone": item.get("telephone"),
                    "image": item.get("image"),
                    "item_type": item.get("@type"),
                    "item_keys": sorted(item.keys()),
                    "raw_item": item,
                }
            )
    return businesses


def parse_dom_cards(html: str, *, base_url: str, limit: int | None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.select('a[href^="/p/"]'):
        href = str(anchor.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        profile_url = urljoin(base_url, href)
        slug = href.removeprefix("/p/").split("~", 1)[0]
        cards.append(
            {
                "slug": slug,
                "title": anchor.get_text(" ", strip=True) or None,
                "profile_url": profile_url,
                "href": href,
            }
        )
        if limit is not None and len(cards) >= limit:
            break
    return cards


def analyze_pagination(session: requests.Session, listing_url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    page_links: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "page=" not in href:
            continue
        match = re.search(r"page=(\d+)", href)
        page_links.append(
            {
                "href": urljoin(listing_url, href),
                "page": int(match.group(1)) if match else None,
                "label": anchor.get_text(strip=True),
            }
        )

    max_linked_page = max(
        (link["page"] for link in page_links if link["page"] is not None), default=1
    )
    page1_slugs = _profile_slugs(html)
    status_p2, html_p2, _ = fetch_html(session, f"{listing_url}?page=2")
    page2_slugs = _profile_slugs(html_p2) if status_p2 == 200 else []

    # Probe a high page to estimate depth without crawling everything.
    status_p20, html_p20, _ = fetch_html(session, f"{listing_url}?page=20")
    page20_count = len(_profile_slugs(html_p20)) if status_p20 == 200 else 0
    status_p100, html_p100, _ = fetch_html(session, f"{listing_url}?page=100")
    page100_count = len(_profile_slugs(html_p100)) if status_p100 == 200 else 0

    return {
        "mechanism": "query_param_page",
        "pagination_links_sample": page_links[:10],
        "max_page_in_nav": max_linked_page,
        "page_1_profile_count": len(page1_slugs),
        "page_2_http_status": status_p2,
        "page_2_profile_count": len(page2_slugs),
        "page_1_and_2_overlap": len(set(page1_slugs) & set(page2_slugs)),
        "page_20_http_status": status_p20,
        "page_20_profile_count": page20_count,
        "page_100_http_status": status_p100,
        "page_100_profile_count": page100_count,
        "recommended_pattern": f"{listing_url}?page=<1-based>",
    }


def _profile_slugs(html: str) -> list[str]:
    slugs: list[str] = []
    for href in re.findall(r'href="(/p/[^"]+)"', html):
        slugs.append(href.removeprefix("/p/").split("~", 1)[0])
    return slugs


def scan_listing_payload_signals(html: str) -> dict[str, Any]:
    geo_pairs = GEO_JSON_RE.findall(html)
    tel_links = re.findall(r'href="tel:([^"]+)"', html)
    phone_literals = re.findall(r'"telephone"\s*:\s*"([^"]+)"', html)
    mobiles = MOBILE_PHONE_RE.findall(html)
    return {
        "geo_coordinates_in_html": len(geo_pairs),
        "geo_sample": [
            {"latitude": float(lat), "longitude": float(lng)} for lat, lng in geo_pairs[:3]
        ],
        "tel_links_in_dom": tel_links[:5],
        "telephone_in_json_ld": phone_literals[:5],
        "mobile_pattern_hits": len(set(mobiles)),
        "coordinates_in_initial_payload": len(geo_pairs) > 0,
        "phones_in_listing_initial_payload": bool(tel_links or phone_literals),
    }


def _meta_tags(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        content = tag.get("content")
        if key and content and isinstance(key, str) and isinstance(content, str):
            meta[key] = content
    return meta


def _extract_opening_hours(html: str, soup: BeautifulSoup) -> dict[str, Any]:
    json_ld_hours: list[Any] = []
    for block in extract_all_json_ld(html):
        hours = block.get("openingHours") or block.get("openingHoursSpecification")
        if hours:
            json_ld_hours.append(hours)
        for node in block.get("@graph", []) if isinstance(block.get("@graph"), list) else []:
            if isinstance(node, dict):
                hours = node.get("openingHours") or node.get("openingHoursSpecification")
                if hours:
                    json_ld_hours.append(hours)

    text_hits = re.findall(
        r"(شنبه|یکشنبه|دوشنبه|سه‌شنبه|سه شنبه|چهارشنبه|پنجشنبه|جمعه)[^\n]{0,40}",
        html,
    )
    return {
        "json_ld_opening_hours": json_ld_hours[:5],
        "weekday_text_hits_sample": text_hits[:8],
        "has_opening_hours": bool(json_ld_hours or text_hits),
    }


def _extract_ratings(html: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    ratings: list[dict[str, Any]] = []
    for block in blocks:
        aggregate = block.get("aggregateRating")
        if isinstance(aggregate, dict):
            ratings.append(aggregate)
        for node in block.get("@graph", []) if isinstance(block.get("@graph"), list) else []:
            if isinstance(node, dict) and isinstance(node.get("aggregateRating"), dict):
                ratings.append(node["aggregateRating"])

    rating_literals = re.findall(
        r'"ratingValue"\s*:\s*"?([0-9.]+)"?',
        html,
    )
    review_counts = re.findall(
        r'"reviewCount"\s*:\s*"?(\d+)"?',
        html,
    )
    return {
        "aggregate_rating_objects": ratings[:5],
        "rating_value_literals": rating_literals[:5],
        "review_count_literals": review_counts[:5],
        "has_rating": bool(ratings or rating_literals),
    }


def _extract_categories_tags(html: str, soup: BeautifulSoup) -> dict[str, Any]:
    category_links = []
    for anchor in soup.select('a[href^="/r/"]'):
        href = str(anchor.get("href") or "")
        text = anchor.get_text(" ", strip=True)
        if href and text:
            category_links.append({"href": href, "text": text})

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in category_links:
        key = f"{row['href']}|{row['text']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    return {
        "category_links": unique[:20],
        "category_link_count": len(unique),
    }


def _extract_images(html: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    images: list[str] = []
    for block in blocks:
        image = block.get("image")
        if isinstance(image, str):
            images.append(image)
        elif isinstance(image, list):
            images.extend(str(x) for x in image if x)
        elif isinstance(image, dict) and image.get("url"):
            images.append(str(image["url"]))

    og_image = None
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if isinstance(og, Tag) and og.get("content"):
        og_image = str(og["content"])
        images.append(og_image)

    img_srcs = [
        str(tag.get("src"))
        for tag in soup.find_all("img")
        if tag.get("src") and "behtarino" in str(tag.get("src"))
    ][:10]

    return {
        "json_ld_images": images[:10],
        "og_image": og_image,
        "img_src_sample": img_srcs,
        "has_images": bool(images or img_srcs),
    }


def _extract_description(html: str, blocks: list[dict[str, Any]], soup: BeautifulSoup) -> dict[str, Any]:
    descriptions: list[str] = []
    for block in blocks:
        desc = block.get("description")
        if isinstance(desc, str) and desc.strip():
            descriptions.append(desc.strip())

    meta_desc = None
    meta = soup.find("meta", attrs={"name": "description"})
    if isinstance(meta, Tag) and meta.get("content"):
        meta_desc = str(meta["content"]).strip()
        descriptions.append(meta_desc)

    og_desc = soup.find("meta", property="og:description")
    if isinstance(og_desc, Tag) and og_desc.get("content"):
        descriptions.append(str(og_desc["content"]).strip())

    unique = list(dict.fromkeys(d for d in descriptions if d))
    return {
        "descriptions": [{"length": len(d), "preview": d[:240]} for d in unique[:5]],
        "meta_description": meta_desc,
        "has_description": bool(unique),
    }


def _extract_social_and_website(soup: BeautifulSoup, html: str) -> dict[str, Any]:
    social_hosts = ("instagram.com", "t.me", "telegram.me", "twitter.com", "x.com", "linkedin.com")
    social: list[str] = []
    websites: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        lowered = href.lower()
        if any(host in lowered for host in social_hosts):
            social.append(href)
        elif lowered.startswith("http") and "behtarino.com" not in lowered:
            websites.append(href)

    same_as = re.findall(r'"sameAs"\s*:\s*(\[[^\]]+\])', html)
    return {
        "social_links": list(dict.fromkeys(social))[:10],
        "external_websites": list(dict.fromkeys(websites))[:10],
        "same_as_json_snippets": same_as[:3],
    }


def _extract_address_phone_geo(html: str, blocks: list[dict[str, Any]], soup: BeautifulSoup) -> dict[str, Any]:
    phones: list[str] = []
    tel_links = [
        a["href"].replace("tel:", "").strip()
        for a in soup.select('a[href^="tel:"]')
        if a.get("href")
    ]
    phones.extend(tel_links)
    phones.extend(MOBILE_PHONE_RE.findall(html))
    phones.extend(LANDLINE_PHONE_RE.findall(html))

    geo_pairs = [
        {"latitude": float(lat), "longitude": float(lng)}
        for lat, lng in GEO_JSON_RE.findall(html)
    ]

    addresses: list[dict[str, Any]] = []
    names: list[str] = []
    for block in blocks:
        name = block.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        address = block.get("address")
        if isinstance(address, dict):
            addresses.append(address)
        tel = block.get("telephone")
        if isinstance(tel, str) and tel.strip():
            phones.append(tel.strip())
        elif isinstance(tel, list):
            phones.extend(str(x) for x in tel if x)

    return {
        "names": list(dict.fromkeys(names))[:5],
        "addresses": addresses[:5],
        "tel_links": tel_links[:10],
        "phones_deduped_sample": list(dict.fromkeys(phones))[:15],
        "geo_coordinates": geo_pairs[:5],
        "has_phone": bool(phones),
        "has_geo": bool(geo_pairs),
        "has_address": bool(addresses),
    }


def probe_detail(session: requests.Session, profile_url: str) -> dict[str, Any]:
    status, html, headers = fetch_html(session, profile_url)
    if status != 200:
        return {
            "profile_url": profile_url,
            "http_status": status,
            "error": "detail fetch failed",
            "anti_bot": detect_anti_bot(status, html, headers),
        }

    soup = BeautifulSoup(html, "html.parser")
    blocks = extract_all_json_ld(html)
    json_ld_summary = summarize_json_ld_blocks(blocks)

    return {
        "profile_url": profile_url,
        "http_status": status,
        "anti_bot": detect_anti_bot(status, html, headers),
        "rendering": detect_rendering_framework(html),
        "json_ld_summary": json_ld_summary,
        "json_ld_blocks_sample": blocks[:3],
        "meta_tags": _meta_tags(soup),
        "core_identity": _extract_address_phone_geo(html, blocks, soup),
        "opening_hours": _extract_opening_hours(html, soup),
        "ratings": _extract_ratings(html, blocks),
        "categories": _extract_categories_tags(html, soup),
        "images": _extract_images(html, blocks),
        "description": _extract_description(html, blocks, soup),
        "social_and_website": _extract_social_and_website(soup, html),
        "available_field_checklist": {
            "name": bool(_extract_address_phone_geo(html, blocks, soup)["names"]),
            "address": bool(_extract_address_phone_geo(html, blocks, soup)["addresses"]),
            "phone": bool(_extract_address_phone_geo(html, blocks, soup)["has_phone"]),
            "geo": bool(_extract_address_phone_geo(html, blocks, soup)["has_geo"]),
            "opening_hours": bool(_extract_opening_hours(html, soup)["has_opening_hours"]),
            "rating": bool(_extract_ratings(html, blocks)["has_rating"]),
            "categories": _extract_categories_tags(html, soup)["category_link_count"] > 0,
            "images": bool(_extract_images(html, blocks)["has_images"]),
            "description": bool(_extract_description(html, blocks, soup)["has_description"]),
            "social_or_website": bool(
                _extract_social_and_website(soup, html)["social_links"]
                or _extract_social_and_website(soup, html)["external_websites"]
            ),
        },
    }


def hunt_api_endpoints(
    session: requests.Session, category: str, city: str | None
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, dict[str, Any] | None]] = [
        (f"{BASE_URL}/api/v1/business/search", {"category": category, "city": city or ""}),
        (f"{BASE_URL}/api/business/search", {"category": category, "city": city or ""}),
        (f"{BASE_URL}/api/v1/rankpage/search", {"tag": category, "city": city or ""}),
        (f"{BASE_URL}/api/v1/search", {"q": category, "city": city or ""}),
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
            "looks_like_json_api": "application/json" in content_type
            and response.status_code == 200,
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


def recommend_strategy(
    anti_bot: dict[str, Any],
    payload_signals: dict[str, Any],
    json_ld_rows: list[dict[str, Any]],
    api_hunt: list[dict[str, Any]],
    detail_field_counts: Counter[str],
) -> str:
    if anti_bot.get("blocked_likely"):
        return "playwright_or_residential_proxy — programmatic requests appear blocked"

    if any(row.get("looks_like_json_api") for row in api_hunt):
        return "internal_json_api — use discovered REST endpoint for pagination"

    rich_detail = detail_field_counts.get("phone", 0) > 0
    if json_ld_rows and payload_signals.get("coordinates_in_initial_payload"):
        if payload_signals.get("phones_in_listing_initial_payload"):
            return "html_json_ld_parsing — listing SSR embeds JSON-LD with geo and phone"
        if rich_detail:
            return (
                "html_json_ld_pagination_plus_detail_fetch — parse SearchResultsPage "
                "JSON-LD per ?page=N for names/geo/urls; fetch /p/{slug} for phones "
                "and extra fields (rating, hours, categories, description)"
            )
        return (
            "html_json_ld_pagination_plus_detail_fetch — listing has geo/urls; "
            "detail pages need deeper inspection for phones"
        )

    return "html_dom_parsing — fall back to visible card anchors"


def print_report(report: dict[str, Any]) -> None:
    anti_bot = report["anti_bot"]
    rendering = report["rendering"]
    payload = report["payload_signals"]
    pagination = report["pagination"]
    field_availability = report["field_availability_across_details"]

    _print_section("BEHTARINO REPAIR SHOPS PROBE — DIAGNOSTIC REPORT")
    print(f"Listing URL .............. {report['listing_url']}")
    print(f"Category / city .......... {report['category_slug']} / {report['city_slug']}")
    print(f"HTTP status .............. {anti_bot['http_status']}")
    print(f"Anti-bot blocked ......... {anti_bot['blocked_likely']}")
    print(f"CDN / server ............. {anti_bot.get('cdn_server')}")
    print(f"Rendering framework ...... {rendering['framework']}")
    print()
    print(f"Cards (JSON-LD) .......... {len(report['listing_sample']['from_json_ld'])}")
    print(f"Cards (DOM /p/ links) .... {len(report['listing_sample']['from_ssr_dom'])}")
    print(
        f"Coords in listing HTML ... {payload['coordinates_in_initial_payload']} "
        f"({payload['geo_coordinates_in_html']} geo tags)"
    )
    print(f"Phones in listing HTML ... {payload['phones_in_listing_initial_payload']}")
    print()
    print(f"Pagination ............... {pagination['mechanism']}")
    print(f"Max page in nav .......... {pagination['max_page_in_nav']}")
    print(f"Page1 / Page2 counts ..... {pagination['page_1_profile_count']} / {pagination['page_2_profile_count']}")
    print(f"Page1∩Page2 overlap ...... {pagination['page_1_and_2_overlap']}")
    print(f"Page20 / Page100 counts .. {pagination['page_20_profile_count']} / {pagination['page_100_profile_count']}")
    print()
    print("Field availability across probed detail pages:")
    for field_name, count in sorted(field_availability.items()):
        print(f"  - {field_name:16s} {count}/{report['detail_probe_count']}")
    print()
    print("API hunt:")
    for row in report["api_hunt"]:
        if row.get("looks_like_json_api"):
            print(f"  ✓ {row['url']}")
        else:
            print(
                f"  ✗ {row.get('url')} status={row.get('http_status')} "
                f"({str(row.get('content_type') or row.get('error', ''))[:40]})"
            )
    print()
    print(f"RECOMMENDED STRATEGY: {report['recommended_strategy']}")
    print(f"Sample saved to: {report['sample_output_path']}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deep probe Behtarino.com car repair shop listings"
    )
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--city", default=DEFAULT_CITY)
    parser.add_argument("--no-city", action="store_true", help="Iran-wide /r/{category}")
    parser.add_argument("--limit", type=int, default=5, help="Listing sample size")
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=3,
        help="How many detail pages to deep-probe",
    )
    parser.add_argument("--skip-detail", action="store_true")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    city = None if args.no_city else args.city
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
        }
    )

    listing_url = build_listing_url(args.category, city)
    print(f"[+] GET {listing_url}")
    status, html, response_headers = fetch_html(session, listing_url)
    anti_bot = detect_anti_bot(status, html, response_headers)

    if anti_bot["blocked_likely"] or status != 200:
        report = {
            "blocked": True,
            "listing_url": listing_url,
            "anti_bot": anti_bot,
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "message": "Listing fetch blocked or non-200; try Playwright or proxy.",
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    rendering = detect_rendering_framework(html)
    json_ld_blocks = extract_all_json_ld(html)
    json_ld_rows = extract_search_results(html)
    dom_rows = parse_dom_cards(html, base_url=listing_url, limit=args.limit)
    if args.limit is not None:
        json_ld_rows_sample = json_ld_rows[: args.limit]
    else:
        json_ld_rows_sample = json_ld_rows

    listing_key_union: set[str] = set()
    for row in json_ld_rows_sample:
        listing_key_union |= set(row.get("item_keys") or [])
        listing_key_union |= _collect_keys(row.get("raw_item") or {})

    payload_signals = scan_listing_payload_signals(html)
    pagination = analyze_pagination(session, listing_url, html)
    api_hunt = hunt_api_endpoints(session, args.category, city)

    detail_probes: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()
    if not args.skip_detail:
        profile_urls: list[str] = []
        for row in json_ld_rows_sample:
            url = row.get("profile_url")
            if isinstance(url, str) and url not in profile_urls:
                profile_urls.append(url)
        for row in dom_rows:
            url = row.get("profile_url")
            if isinstance(url, str) and url not in profile_urls:
                profile_urls.append(url)

        for profile_url in profile_urls[: max(0, args.detail_limit)]:
            print(f"[+] Detail probe: {profile_url}")
            detail = probe_detail(session, profile_url)
            detail_probes.append(detail)
            checklist = detail.get("available_field_checklist") or {}
            for field_name, present in checklist.items():
                if present:
                    field_counts[field_name] += 1

    recommended = recommend_strategy(
        anti_bot, payload_signals, json_ld_rows_sample, api_hunt, field_counts
    )

    report: dict[str, Any] = {
        "blocked": False,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "listing_url": listing_url,
        "category_slug": args.category,
        "city_slug": city,
        "url_pattern_note": (
            "Behtarino uses /r/{category-slug}/{city-slug} with Persian slugs; "
            "pagination via ?page=N (1-based)."
        ),
        "anti_bot": anti_bot,
        "rendering": rendering,
        "listing_json_ld_summary": summarize_json_ld_blocks(json_ld_blocks),
        "listing_item_key_union": sorted(listing_key_union),
        "payload_signals": payload_signals,
        "pagination": pagination,
        "api_hunt": api_hunt,
        "listing_sample": {
            "from_json_ld": [
                {k: v for k, v in row.items() if k != "raw_item"}
                for row in json_ld_rows_sample
            ],
            "from_json_ld_raw_first": (
                json_ld_rows_sample[0].get("raw_item") if json_ld_rows_sample else None
            ),
            "from_ssr_dom": dom_rows,
            "total_json_ld_on_page_1": len(json_ld_rows),
        },
        "detail_probes": detail_probes,
        "detail_probe_count": len(detail_probes),
        "field_availability_across_details": dict(field_counts),
        "recommended_strategy": recommended,
        "crawler_field_plan": {
            "from_listing_json_ld": [
                "name",
                "profile_url",
                "slug",
                "latitude",
                "longitude",
                "street_address",
                "address_locality",
                "address_region",
                "postal_code",
                "image",
                "position",
                "listing_page",
            ],
            "from_detail_page": [
                "phones",
                "opening_hours",
                "rating_value",
                "review_count",
                "description",
                "categories",
                "images",
                "social_links",
                "external_website",
            ],
            "always": ["source", "scraped_at", "category", "city", "raw_payload"],
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["sample_output_path"] = str(output_path)

    print_report(report)
    if args.verbose:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
