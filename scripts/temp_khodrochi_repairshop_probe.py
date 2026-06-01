"""
Temporary probe: Khodrochi repair shop directory (listing + detail).

Usage:
  python scripts/temp_khodrochi_repairshop_probe.py
  python scripts/temp_khodrochi_repairshop_probe.py --limit 5 --probe-detail

Dependencies:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://khodrochi.ir/repairshop/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch(session: requests.Session, url: str, *, timeout: float = 30) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def analyze_pagination(html: str, page_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.find("div", class_="page-nave-repairshop")

    links: list[dict[str, str]] = []
    if nav:
        for a in nav.find_all("a", href=True):
            links.append({"text": a.get_text(strip=True), "href": a["href"]})

    mechanism = "unknown"
    if links and any("page=" in ln["href"] for ln in links):
        mechanism = "query_param_page"
    elif soup.find(string=re.compile(r"صفحه\s*\d+")):
        mechanism = "link_navigation"

    # No AJAX hints in listing HTML for card load
    has_ajax_load = bool(
        re.search(r"\.load\s*\(|fetch\s*\(|XMLHttpRequest", html, re.I)
    )

    return {
        "page_url": page_url,
        "mechanism": mechanism,
        "is_ajax_lazy_load": False,
        "has_ajax_hints_in_html": has_ajax_load,
        "nav_links": links,
        "sample_next_href": links[-1]["href"] if links else None,
        "cards_per_page": len(soup.select("a.repairshop")),
    }


def parse_listing_cards(html: str, *, base_url: str, limit: int | None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []

    for a in soup.select("a.repairshop"):
        title_el = a.select_one(".rs_title span")
        name = title_el.get_text(strip=True) if title_el else None

        verified = bool(a.select_one(".fa-crown"))

        excerpt_el = a.select_one(".rs_excerpt")
        excerpt = excerpt_el.get_text(strip=True) if excerpt_el else None

        tags = [t.get_text(strip=True) for t in a.select(".rs_field")]

        address_el = a.select_one(".rs_address")
        address = address_el.get_text(" ", strip=True) if address_el else None

        href = a.get("href", "")
        detail_url = urljoin(base_url, href) if href else None
        item_id = None
        if href:
            qs = parse_qs(urlparse(urljoin(base_url, href)).query)
            item_id = qs.get("id", [None])[0] or re.search(r"id=(\d+)", href)
            if isinstance(item_id, re.Match):
                item_id = item_id.group(1)

        img_el = a.select_one("img.rs_img")
        image_url = urljoin(base_url, img_el["src"]) if img_el and img_el.get("src") else None

        # Phone is not on listing cards; only on detail pages
        tel = None
        tel_a = a.select_one('a[href^="tel:"]')
        if tel_a:
            tel = tel_a["href"].replace("tel:", "").strip()

        cards.append(
            {
                "name": name,
                "specialty_tags": tags,
                "primary_phone": tel,
                "detail_url": detail_url,
                "item_id": item_id,
                "verified": verified,
                "excerpt": excerpt,
                "address_listing": address,
                "image_url": image_url,
            }
        )
        if limit is not None and len(cards) >= limit:
            break

    return cards


def probe_detail(session: requests.Session, detail_url: str) -> dict[str, Any]:
    html = fetch(session, detail_url)
    soup = BeautifulSoup(html, "html.parser")

    info: dict[str, str] = {}
    for item in soup.select(".rsi_list_item.rsi_list_item2 div"):
        text = item.get_text(strip=True)
        if text.startswith("- "):
            text = text[2:]
        if ":" in text:
            key, _, val = text.partition(":")
            info[key.strip()] = val.strip()

    tel_a = soup.select_one('a.spec_col_link.scl2[href^="tel:"], a[href^="tel:"].rsil')
    phone = tel_a["href"].replace("tel:", "").strip() if tel_a else info.get("شماره تماس")

    maps_a = soup.select_one('a.spec_col_link.scl4[href*="google.com/maps"]')
    lat, lng = None, None
    if maps_a:
        m = re.search(r"query=([-\d.]+),([-\d.]+)", maps_a["href"])
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))

    gallery = [
        urljoin(detail_url, img["href"])
        for img in soup.select("a.rsi_list_item_img.gallery[href]")
    ]

    services: dict[str, list[str]] = {}
    for block in soup.select(".rsi__serv_list"):
        title_el = block.select_one(".rsi_title")
        if not title_el:
            continue
        cat = title_el.get_text(strip=True)
        offered = []
        for row in block.select(".rsi_list_item_service"):
            icon = row.select_one("i")
            if icon and "fa-check-circle" in (icon.get("class") or []):
                div = row.find("div")
                if div:
                    offered.append(div.get_text(strip=True))
        if offered:
            services[cat] = offered

    social: dict[str, str] = {}
    for link in soup.select(".spec_col_link_box a.spec_col_link"):
        href = link.get("href", "")
        label = link.get_text(strip=True)
        if "instagram" in href:
            social["instagram"] = href
        elif href.startswith("tel:"):
            social["phone_link"] = href
        elif "google.com/maps" in href:
            social["maps"] = href
        elif href.startswith("http"):
            social.setdefault("website", href)

    brand_mentions = re.findall(
        r"برند[s]?\s*[:：]\s*([^<\n]+)|نمایندگی\s+(\w+)",
        html,
        re.I,
    )

    return {
        "detail_url": detail_url,
        "title": soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else None,
        "structured_info": info,
        "primary_phone": phone,
        "coordinates": {"lat": lat, "lng": lng} if lat is not None else None,
        "gallery_urls": gallery,
        "gallery_count": len(gallery),
        "services_offered": services,
        "social_links": social,
        "has_car_brand_section": bool(soup.select(".br, .brand, [class*=brand]")),
        "brand_regex_hits": [b for pair in brand_mentions for b in pair if b],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Khodrochi repairshop probe")
    parser.add_argument("--limit", type=int, default=5, help="Cards to extract from page 1")
    parser.add_argument("--probe-detail", action="store_true", default=True)
    parser.add_argument("--no-probe-detail", action="store_false", dest="probe_detail")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    html = fetch(session, BASE)
    pagination = analyze_pagination(html, BASE)
    cards = parse_listing_cards(html, base_url=BASE, limit=args.limit)

    out: dict[str, Any] = {
        "pagination_analysis": pagination,
        "listing_sample": cards,
    }

    if args.probe_detail and cards:
        first_url = cards[0].get("detail_url")
        if first_url:
            out["detail_probe"] = probe_detail(session, first_url)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
