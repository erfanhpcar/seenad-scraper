"""
Temporary probe: Bama.ir car dealers directory (listing + detail).

Usage:
  python scripts/temp_bama_dealers_probe.py
  python scripts/temp_bama_dealers_probe.py --limit 5 --dealer-id 749

Dependencies:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://bama.ir/dealer/car"
SEARCH_API = "https://bama.ir/prf/api/Corporation/search"
DETAIL_API = "https://bama.ir/prf/api/Corporation"
DETAIL_ABOUT_API = "https://bama.ir/prf/api/Corporation/{dealer_id}/details"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_PAGE_SIZE = 24


def fetch_html(session: requests.Session, url: str, *, timeout: float = 30) -> tuple[int, str]:
    response = session.get(url, timeout=timeout)
    return response.status_code, response.text


def fetch_json(
    session: requests.Session, url: str, *, params: dict[str, Any] | None = None, timeout: float = 30
) -> tuple[int, Any]:
    response = session.get(url, params=params, timeout=timeout, headers={"Accept": "application/json"})
    if response.status_code != 200:
        return response.status_code, {"raw": response.text[:500]}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.text[:500]}


def analyze_rendering_and_pagination(
    session: requests.Session, html: str, listing_url: str
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    ssr_cards = soup.select(".dealer-list-card")
    has_nuxt_state = "window.__NUXT__=" in html
    has_next_data = "__NEXT_DATA__" in html
    observer = bool(soup.select(".observer-target"))

    page1_ids = _dealer_ids_from_dom(html)
    status_p2, html_p2 = fetch_html(session, f"{listing_url}?page=2")
    page2_ids = _dealer_ids_from_dom(html_p2) if status_p2 == 200 else []

    status_api, api_page0 = fetch_json(
        session,
        SEARCH_API,
        params={"CorporationType": "car", "pageIndex": 0, "pageSize": 5},
    )
    status_api_p1, api_page1 = fetch_json(
        session,
        SEARCH_API,
        params={"CorporationType": "car", "pageIndex": 1, "pageSize": 5},
    )

    api_meta: dict[str, Any] | None = None
    api_page0_ids: list[int] = []
    api_page1_ids: list[int] = []
    if status_api == 200 and isinstance(api_page0, dict):
        api_meta = api_page0.get("metadata")
        api_page0_ids = [row["id"] for row in api_page0.get("data", []) if isinstance(row, dict)]
    if status_api_p1 == 200 and isinstance(api_page1, dict):
        api_page1_ids = [row["id"] for row in api_page1.get("data", []) if isinstance(row, dict)]

    mechanism = "unknown"
    if observer and api_meta:
        mechanism = "infinite_scroll_via_prf_api"
    elif ssr_cards:
        mechanism = "ssr_dom_only"

    return {
        "listing_url": listing_url,
        "http_status_listing": 200,
        "rendering": {
            "framework": "nuxt_ssr",
            "cards_in_initial_dom": len(ssr_cards),
            "has_window___NUXT__": has_nuxt_state,
            "has___NEXT_DATA__": has_next_data,
            "nuxt_state_format": "function_payload_not_plain_json" if has_nuxt_state else None,
            "primary_card_selector": ".dealer-list-card",
        },
        "pagination": {
            "mechanism": mechanism,
            "query_param_page_2_same_as_page_1": page1_ids[:5] == page2_ids[:5] if page2_ids else None,
            "observer_target_present": observer,
            "recommended_api": SEARCH_API,
            "recommended_params": {
                "CorporationType": "car",
                "pageIndex": "<0-based>",
                "pageSize": DEFAULT_PAGE_SIZE,
            },
            "api_metadata_sample": api_meta,
            "api_page0_first_ids": api_page0_ids,
            "api_page1_first_ids": api_page1_ids,
            "note": "?page=N on /dealer/car does not change SSR payload; use pageIndex on search API.",
        },
    }


def _dealer_ids_from_dom(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids: list[str] = []
    for anchor in soup.select("a.dealer-list-card__link"):
        match = re.search(r"/dealer/(\d+)", anchor.get("href", ""))
        if match:
            ids.append(match.group(1))
    return ids


def parse_listing_cards_dom(html: str, *, limit: int | None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []

    for card in soup.select(".dealer-list-card"):
        link = card.select_one("a.dealer-list-card__link")
        profile_path = link["href"] if link and link.get("href") else None
        dealer_id = None
        if profile_path:
            match = re.search(r"/dealer/(\d+)", profile_path)
            dealer_id = int(match.group(1)) if match else None

        title_el = card.select_one(".dealer-list-card__details-header-title")
        address_el = card.select_one(".dealer-list-card__details-address")
        registered_el = card.select_one(".dealer-list-card__details-registeredAt")
        badge_el = card.select_one(".dealer-list-card__details-header-badge-text")
        stock_el = card.select_one(".dealer-list-card__details-button-text")
        banner_el = card.select_one(".dealer-list-card__banner-image")
        logo_el = card.select_one(".dealer-list-card__logo-image")

        stock_count = None
        if stock_el:
            digits = re.sub(r"[^\d]", "", stock_el.get_text(strip=True))
            stock_count = int(digits) if digits else None

        cards.append(
            {
                "dealer_id": dealer_id,
                "title": title_el.get_text(strip=True) if title_el else None,
                "profile_url": urljoin(LISTING_URL, profile_path) if profile_path else None,
                "address_snapshot": address_el.get_text(strip=True) if address_el else None,
                "member_since_text": registered_el.get_text(strip=True) if registered_el else None,
                "badge": badge_el.get_text(strip=True) if badge_el else None,
                "active_ads_count_text": stock_el.get_text(strip=True) if stock_el else None,
                "active_ads_count": stock_count,
                "banner_image_url": banner_el.get("src") if banner_el else None,
                "logo_image_url": logo_el.get("src") if logo_el else None,
                "source": "ssr_dom",
            }
        )
        if limit is not None and len(cards) >= limit:
            break

    return cards


def fetch_listing_api(
    session: requests.Session, *, page_index: int = 0, page_size: int = 5
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None]:
    status, payload = fetch_json(
        session,
        SEARCH_API,
        params={"CorporationType": "car", "pageIndex": page_index, "pageSize": page_size},
    )
    if status != 200 or not isinstance(payload, dict):
        return status, [], None

    rows: list[dict[str, Any]] = []
    for row in payload.get("data", []):
        if not isinstance(row, dict):
            continue
        registered = row.get("registeredAt") if isinstance(row.get("registeredAt"), dict) else {}
        tag = row.get("tag") if isinstance(row.get("tag"), dict) else None
        rows.append(
            {
                "dealer_id": row.get("id"),
                "title": row.get("title"),
                "profile_url": urljoin(LISTING_URL, f"/dealer/{row.get('id')}"),
                "address_snapshot": row.get("address"),
                "member_since_persian": registered.get("persian"),
                "member_since_gregorian": registered.get("gregorian"),
                "badge": tag.get("displayName") if tag else None,
                "badge_value": tag.get("value") if tag else None,
                "active_ads_count": row.get("adsCount"),
                "status": row.get("status"),
                "banner_image_url": row.get("banner"),
                "logo_image_url": row.get("logo"),
                "source": "prf_api",
            }
        )
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    return status, rows, metadata


def probe_detail(session: requests.Session, dealer_id: int) -> dict[str, Any]:
    profile_url = urljoin(LISTING_URL, f"/dealer/{dealer_id}")
    status_html, html = fetch_html(session, profile_url)

    dom: dict[str, Any] = {
        "profile_url": profile_url,
        "http_status": status_html,
        "title": None,
        "address": None,
        "active_ads_count_text": None,
        "phone_tel_links": [],
        "call_button_present": False,
    }
    if status_html == 200:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one(".dealer-detail__top-header-title")
        address_el = soup.select_one(".dealer-detail__address")
        ads_title_el = soup.select_one(".dealer-detail__ads-title")
        dom["title"] = title_el.get_text(strip=True) if title_el else None
        dom["address"] = address_el.get_text(strip=True) if address_el else None
        dom["active_ads_count_text"] = ads_title_el.get_text(strip=True) if ads_title_el else None
        dom["phone_tel_links"] = [
            anchor["href"].replace("tel:", "").strip()
            for anchor in soup.select('a[href^="tel:"]')
        ]
        dom["call_button_present"] = bool(soup.select(".dealer-detail__button--call"))

    status_main, main_payload = fetch_json(session, f"{DETAIL_API}/{dealer_id}")
    status_about, about_payload = fetch_json(session, DETAIL_ABOUT_API.format(dealer_id=dealer_id))

    api_main: dict[str, Any] | None = None
    if status_main == 200 and isinstance(main_payload, dict):
        data = main_payload.get("data")
        if isinstance(data, dict):
            contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
            ads_stats = data.get("adsStatistics") if isinstance(data.get("adsStatistics"), dict) else {}
            car_stats = ads_stats.get("car") if isinstance(ads_stats.get("car"), dict) else {}
            api_main = {
                "dealer_id": data.get("id"),
                "title": data.get("title"),
                "address": data.get("address"),
                "phones": contact.get("landlineNumbers"),
                "activity_duration": data.get("activityDuration"),
                "coordinates": data.get("coordinates"),
                "social_networks": data.get("socialNetworks"),
                "active_car_ads_count": car_stats.get("count"),
                "car_brands_count": len(car_stats.get("brands") or []),
                "registered_at": data.get("registeredAt"),
                "tag": data.get("tag"),
                "manager_name": None,
            }

    api_about: dict[str, Any] | None = None
    if status_about == 200 and isinstance(about_payload, dict):
        data = about_payload.get("data")
        if isinstance(data, dict):
            members = data.get("members")
            manager_name = None
            if isinstance(members, list) and members:
                first = members[0]
                if isinstance(first, dict):
                    manager_name = first.get("name") or first.get("fullName") or first.get("title")
            api_about = {
                "about_us": data.get("aboutUs"),
                "faq": data.get("faq"),
                "gallery": data.get("gallery"),
                "members": members,
                "manager_name_from_members": manager_name,
            }
            if api_main is not None and manager_name:
                api_main["manager_name"] = manager_name

    return {
        "dom_probe": dom,
        "api_probe": {
            "main_endpoint": f"{DETAIL_API}/{{id}}",
            "main_http_status": status_main,
            "main": api_main,
            "about_endpoint": DETAIL_ABOUT_API.format(dealer_id="{id}"),
            "about_http_status": status_about,
            "about": api_about,
        },
        "field_locations": {
            "phone_numbers": "GET /prf/api/Corporation/{id} -> data.contact.landlineNumbers (not in listing DOM)",
            "full_address": "SSR .dealer-detail__address and API data.address",
            "active_inventory_count": "SSR .dealer-detail__ads-title and API data.adsStatistics.car.count",
            "manager": "Optional: GET /prf/api/Corporation/{id}/details -> data.members (often null)",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bama.ir car dealers directory probe")
    parser.add_argument("--limit", type=int, default=5, help="Dealers to sample from listing")
    parser.add_argument(
        "--dealer-id",
        type=int,
        default=None,
        help="Dealer id for detail probe (default: first listing id)",
    )
    parser.add_argument("--skip-detail", action="store_true", help="Skip detail page/API probe")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    status, html = fetch_html(session, LISTING_URL)
    if status == 403:
        print(
            json.dumps(
                {
                    "blocked": True,
                    "http_status": 403,
                    "message": "Anti-bot/WAF blocked requests; try Playwright or residential proxy.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    if status != 200:
        print(
            json.dumps(
                {"blocked": False, "http_status": status, "message": "Unexpected listing HTTP status."},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    api_status, api_rows, api_meta = fetch_listing_api(session, page_index=0, page_size=args.limit)
    dom_rows = parse_listing_cards_dom(html, limit=args.limit)

    out: dict[str, Any] = {
        "blocked": False,
        "inspection": analyze_rendering_and_pagination(session, html, LISTING_URL),
        "listing_sample": {
            "from_ssr_dom": dom_rows,
            "from_prf_api": api_rows,
            "api_http_status": api_status,
            "api_metadata": api_meta,
        },
    }

    detail_id = args.dealer_id
    if detail_id is None:
        if api_rows and api_rows[0].get("dealer_id"):
            detail_id = int(api_rows[0]["dealer_id"])
        elif dom_rows and dom_rows[0].get("dealer_id"):
            detail_id = int(dom_rows[0]["dealer_id"])

    if not args.skip_detail and detail_id is not None:
        out["detail_probe"] = probe_detail(session, detail_id)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
