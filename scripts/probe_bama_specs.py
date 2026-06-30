#!/usr/bin/env python3
"""
Lightweight probe: Bama.ir car technical specifications page.

Target URL:
  https://bama.ir/car-reviews/arya/at-specs-1642-2.0liter

Fetches the page and reports:
  1. __NEXT_DATA__ JSON (Next.js) if present
  2. __NUXT__ / specsData payload (Nuxt.js — what bama.ir actually uses)
  3. DOM fallback via .spec-header / .spec-item selectors

Usage:
  python scripts/probe_bama_specs.py
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

TARGET_URL = "https://bama.ir/car-reviews/arya/at-specs-1642-2.0liter"
REQUEST_TIMEOUT_S = 20

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://bama.ir/",
}

SPEC_KEY_HINTS = ("spec", "technical", "specs", "specification")

# Nuxt minified payload: specsData:[{title:"...", items:[{key:"...", value:...}]}]
_NUXT_SPECS_SECTION_RE = re.compile(
    r'\{title:"([^"]+)",icon:"([^"]*)",icon_url:"[^"]*",items:\[(.*?)\]\}',
    re.DOTALL,
)
_NUXT_ITEM_TEXT_RE = re.compile(r'\{key:"([^"]+)",value:"([^"]*)",type:')
_NUXT_ITEM_REF_RE = re.compile(r'\{key:"([^"]+)",value:([a-zA-Z_$][\w$]*),type:')


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _fetch_page() -> requests.Response | None:
    print(f"[+] GET {TARGET_URL}")
    print(f"[+] Timeout: {REQUEST_TIMEOUT_S}s")

    try:
        response = requests.get(
            TARGET_URL,
            headers=BROWSER_HEADERS,
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
    except SSLError as exc:
        print(f"[!] SSL error: {exc}")
        return None
    except (Timeout, ReadTimeout) as exc:
        print(f"[!] Request timed out after {REQUEST_TIMEOUT_S}s: {exc}")
        return None
    except ConnectionError as exc:
        print(f"[!] Connection error: {exc}")
        return None
    except RequestException as exc:
        print(f"[!] Request failed: {exc}")
        return None

    print(f"[=] Status: {response.status_code}")
    print(f"[=] Final URL: {response.url}")
    print(f"[=] Content-Type: {response.headers.get('Content-Type', '(not set)')}")
    print(f"[=] Body length: {len(response.content):,} bytes")

    if response.status_code != 200:
        preview = response.text[:500].replace("\n", " ")
        print(f"[!] Non-200 response. Body preview: {preview!r}")

    return response


def _key_matches_hint(key: str) -> bool:
    lower = key.lower()
    return any(hint in lower for hint in SPEC_KEY_HINTS)


def _find_spec_related_nodes(
    obj: Any,
    path: str = "$",
    hits: list[tuple[str, Any]] | None = None,
    depth: int = 0,
    max_depth: int = 12,
) -> list[tuple[str, Any]]:
    """Walk a JSON tree and collect paths whose keys mention specs/technical."""
    if hits is None:
        hits = []
    if depth > max_depth:
        return hits

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if _key_matches_hint(str(key)):
                hits.append((child_path, value))
            _find_spec_related_nodes(value, child_path, hits, depth + 1, max_depth)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            _find_spec_related_nodes(item, f"{path}[{index}]", hits, depth + 1, max_depth)

    return hits


def _probe_next_data(soup: BeautifulSoup) -> bool:
    _print_section("Jackpot path: __NEXT_DATA__ (Next.js)")
    script = soup.find("script", id="__NEXT_DATA__", attrs={"type": "application/json"})
    if script is None:
        print("[i] No <script id=\"__NEXT_DATA__\"> tag found.")
        return False

    raw = (script.string or script.get_text() or "").strip()
    if not raw:
        print("[!] __NEXT_DATA__ tag is empty.")
        return False

    print(f"[+] Found __NEXT_DATA__ ({len(raw):,} chars)")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[!] Failed to parse __NEXT_DATA__ JSON: {exc}")
        return False

    top_keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
    print(f"[i] Top-level keys: {top_keys}")

    hits = _find_spec_related_nodes(payload)
    if not hits:
        print(f"[i] No keys matching {SPEC_KEY_HINTS} in __NEXT_DATA__ tree.")
        return True

    print(f"[+] Found {len(hits)} spec-related key path(s):")
    for path, value in hits[:20]:
        preview = json.dumps(value, ensure_ascii=False, indent=2)
        if len(preview) > 1200:
            preview = preview[:1200] + "\n... (truncated)"
        print(f"\n--- {path} ---")
        print(preview)

    if len(hits) > 20:
        print(f"\n[i] ... and {len(hits) - 20} more paths omitted.")

    return True


def _extract_nuxt_specs_from_html(html: str) -> list[dict[str, Any]] | None:
    """Parse specsData sections from the Nuxt __NUXT__ inline assignment."""
    anchor = html.find("specsData:[")
    if anchor == -1:
        return None

    slice_start = html.rfind("{", 0, anchor)
    if slice_start == -1:
        slice_start = anchor

    # Limit scan window — specsData sits in a bounded chunk of the payload.
    window = html[slice_start:slice_start + 120_000]
    sections: list[dict[str, Any]] = []

    for title, icon, items_blob in _NUXT_SPECS_SECTION_RE.findall(window):
        items: list[dict[str, str]] = []
        for key, value in _NUXT_ITEM_TEXT_RE.findall(items_blob):
            items.append({"key": key, "value": value, "type": "text"})
        for key, ref in _NUXT_ITEM_REF_RE.findall(items_blob):
            if any(item["key"] == key for item in items):
                continue
            items.append({"key": key, "value": f"<nuxt-ref:{ref}>", "type": "reference"})
        sections.append({"title": title, "icon": icon, "items": items})

    return sections or None


def _probe_nuxt_data(html: str) -> bool:
    _print_section("Jackpot path: __NUXT__ / specsData (Nuxt.js)")
    if "__NUXT__" not in html:
        print("[i] No __NUXT__ assignment in page (site may not use Nuxt).")
        return False

    print("[+] Found window.__NUXT__ inline script payload.")
    if "specsData:[" not in html:
        print("[i] __NUXT__ present but specsData array not found in raw HTML.")
        return False

    sections = _extract_nuxt_specs_from_html(html)
    if not sections:
        print("[!] specsData anchor found but section regex did not match.")
        return False

    total_items = sum(len(s["items"]) for s in sections)
    print(f"[+] Extracted {len(sections)} spec sections, {total_items} items from specsData.")

    sample = {
        "source": "nuxt_specsData",
        "section_count": len(sections),
        "item_count": total_items,
        "sections": sections[:3],
    }
    print("\n--- Sample (first 3 sections) ---")
    print(json.dumps(sample, ensure_ascii=False, indent=2))

    if len(sections) > 3:
        print(f"\n[i] ... {len(sections) - 3} more sections in payload.")

    return True


def _spec_item_value(item: Tag) -> str:
    value_el = item.select_one(".spec-item__value")
    if value_el is not None:
        return value_el.get_text(strip=True)

    svg = item.select_one("svg")
    if svg is not None:
        classes = svg.get("class") or []
        if "check-icon" in classes:
            return "true"
        if "close-icon" in classes:
            return "false"

    return ""


def _extract_specs_from_dom(soup: BeautifulSoup) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []

    for header in soup.select(".spec-header"):
        title_el = header.select_one(".spec-header__title")
        if title_el is None:
            continue

        container = header.parent
        if container is None:
            continue

        items: list[dict[str, str]] = []
        for spec_item in container.select(".spec-item"):
            key_el = spec_item.select_one(".spec-item__title")
            if key_el is None:
                continue
            key = key_el.get_text(strip=True)
            value = _spec_item_value(spec_item)
            item_type = "boolean" if spec_item.select_one("svg") and not spec_item.select_one(
                ".spec-item__value"
            ) else "text"
            items.append({"key": key, "value": value, "type": item_type})

        if items:
            icon_el = header.select_one(".spec-header__icon-holder img")
            sections.append(
                {
                    "title": title_el.get_text(strip=True),
                    "icon_src": icon_el.get("src") if icon_el else None,
                    "items": items,
                }
            )

    return sections


def _probe_dom(soup: BeautifulSoup) -> None:
    _print_section("DOM fallback: .spec-header / .spec-item")
    sections = _extract_specs_from_dom(soup)

    if not sections:
        print("[!] No spec sections found via DOM selectors.")
        print("[i] Tried: .spec-header, .spec-header__title, .spec-item, .spec-item__title")
        return

    total_items = sum(len(s["items"]) for s in sections)
    print(f"[+] Parsed {len(sections)} sections, {total_items} items from SSR DOM.")

    sample = {
        "source": "dom_ssr",
        "section_count": len(sections),
        "item_count": total_items,
        "sections": sections,
    }
    print(json.dumps(sample, ensure_ascii=False, indent=2))


def probe() -> int:
    response = _fetch_page()
    if response is None:
        return 1
    if response.status_code != 200:
        return 1

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    found_json = _probe_next_data(soup)
    found_nuxt = _probe_nuxt_data(html)

    if not found_json and not found_nuxt:
        print("\n[i] No embedded JSON jackpot path succeeded — falling back to DOM parsing.")
    else:
        print("\n[i] Embedded payload found; DOM extraction shown for comparison.")

    _probe_dom(soup)

    print("\n[+] Probe complete.")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
