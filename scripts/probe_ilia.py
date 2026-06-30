#!/usr/bin/env python3
"""
Lightweight probe: Ilia Auto (ایلیا موتور) sales & service centers page.

Target URL:
  https://ilia-auto.com/%d9%85%d8%b1%d8%a7%da%a9%d8%b2-%d9%81%d8%b1%d9%88%d8%b4-%d9%88-%d8%ae%d8%af%d9%85%d8%a7%d8%aa/

Fetches the page, saves raw HTML, and prints a structural summary to help
decide whether to scrape HTML or hunt for a hidden API.

Usage:
  python scripts/probe_ilia.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

TARGET_URL = (
    "https://ilia-auto.com/"
    "%d9%85%d8%b1%d8%a7%da%a9%d8%b2-%d9%81%d8%b1%d9%88%d8%b4-%d9%88-%d8%ae%d8%af%d9%85%d8%a7%d8%aa/"
)
RAW_HTML_PATH = Path("data/ilia_probe_raw.html")
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
    # Omit "br" — requests needs optional brotli support to decode it.
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# Class-name substrings that often wrap dealer/location blocks.
DEALER_CONTAINER_HINTS = (
    "dealer",
    "agency",
    "branch",
    "showroom",
    "service",
    "location",
    "map",
    "card",
    "accordion",
    "office",
    "represent",
    "نمایند",
    "شعبه",
    "مراکز",
)

SCRIPT_KEYWORDS = ("lat", "lng", "dealer", "agency", "map")
JSON_LD_TYPE = re.compile(r'"@type"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _fetch_page() -> requests.Response:
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
        raise SystemExit(1) from exc
    except (Timeout, ReadTimeout) as exc:
        print(f"[!] Request timed out after {REQUEST_TIMEOUT_S}s: {exc}")
        raise SystemExit(1) from exc
    except ConnectionError as exc:
        print(f"[!] Connection error: {exc}")
        raise SystemExit(1) from exc
    except RequestException as exc:
        print(f"[!] Request failed: {exc}")
        raise SystemExit(1) from exc

    print(f"[=] Status: {response.status_code}")
    print(f"[=] Final URL: {response.url}")
    print(f"[=] Content-Type: {response.headers.get('Content-Type', '(not set)')}")
    print(f"[=] Body length: {len(response.content):,} bytes")

    if response.status_code != 200:
        preview = response.text[:500].replace("\n", " ")
        print(f"[!] Non-200 response. Body preview: {preview!r}")
        raise SystemExit(1)

    content_encoding = response.headers.get("Content-Encoding", "")
    if content_encoding == "br" or not response.text.lstrip().startswith(("<!DOCTYPE", "<html", "<HTML")):
        print(
            "[!] Response does not look like decoded HTML "
            f"(Content-Encoding={content_encoding!r}). "
            "Retry without brotli or install brotli support."
        )
        raise SystemExit(1)

    return response


def _save_raw_html(html: str) -> None:
    RAW_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_HTML_PATH.write_text(html, encoding="utf-8")
    print(f"[+] Saved raw HTML to {RAW_HTML_PATH.resolve()}")


def _count_dealer_like_containers(soup: BeautifulSoup) -> dict[str, int]:
    """Count semantic tags and class/id hints that may hold dealer rows."""
    counts: dict[str, int] = {
        "table": len(soup.find_all("table")),
        "ul": len(soup.find_all("ul")),
        "article": len(soup.find_all("article")),
        "details_accordion": len(soup.find_all("details")),
    }

    card_like = 0
    hint_hits: dict[str, int] = {}

    for tag in soup.find_all(True):
        classes = tag.get("class") or []
        class_str = " ".join(str(c) for c in classes).lower()
        tag_id = (tag.get("id") or "").lower()
        combined = f"{class_str} {tag_id}"

        if not combined.strip():
            continue

        matched = [hint for hint in DEALER_CONTAINER_HINTS if hint in combined]
        if not matched:
            continue

        card_like += 1
        for hint in matched:
            hint_hits[hint] = hint_hits.get(hint, 0) + 1

    counts["dealer_hint_elements"] = card_like
    counts["dealer_hint_breakdown"] = hint_hits
    return counts


def _scan_scripts(soup: BeautifulSoup, raw_html: str) -> None:
    scripts = soup.find_all("script")
    print(f"[i] Total <script> tags: {len(scripts)}")

    keyword_hits: list[tuple[int, str, list[str]]] = []
    for index, script in enumerate(scripts):
        content = script.string or ""
        src = script.get("src") or ""
        blob = f"{content}\n{src}".lower()
        hits = [kw for kw in SCRIPT_KEYWORDS if kw in blob]
        if hits:
            keyword_hits.append((index, src or "(inline)", hits))

    if keyword_hits:
        print(f"[!] Scripts mentioning {SCRIPT_KEYWORDS}:")
        for index, source, hits in keyword_hits:
            print(f"    script #{index} [{', '.join(hits)}] — {source[:120]}")
            if source == "(inline)":
                inline = scripts[index].string or ""
                for line in inline.splitlines():
                    lower = line.lower()
                    if any(kw in lower for kw in SCRIPT_KEYWORDS):
                        print(f"        -> {line.strip()[:140]}")
    else:
        print(f"[i] No inline/external script src matched keywords {SCRIPT_KEYWORDS}.")

    # JSON-LD blocks
    json_ld_blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
    if json_ld_blocks:
        print(f"[i] application/ld+json blocks: {len(json_ld_blocks)}")
        for block_index, block in enumerate(json_ld_blocks):
            text = (block.string or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                print(f"    block #{block_index}: invalid JSON ({len(text)} chars)")
                continue
            types = JSON_LD_TYPE.findall(text)
            print(f"    block #{block_index}: parsed OK, @type hints: {types or ['(none)']}")

    # Loose coordinate patterns anywhere in HTML
    lat_matches = re.findall(r'"lat(?:itude)?"\s*:\s*[-]?\d+\.?\d*', raw_html, re.I)
    lng_matches = re.findall(r'"lng(?:itude)?"\s*:\s*[-]?\d+\.?\d*', raw_html, re.I)
    if lat_matches or lng_matches:
        print(f"[!] Coordinate-like JSON keys in raw HTML: lat={len(lat_matches)}, lng={len(lng_matches)}")
        for sample in (lat_matches + lng_matches)[:5]:
            print(f"        {sample}")


def _print_structure_summary(soup: BeautifulSoup, raw_html: str) -> None:
    _print_section("HTML structure")

    counts = _count_dealer_like_containers(soup)
    print(f"[i] <table> tags: {counts['table']}")
    print(f"[i] <ul> lists: {counts['ul']}")
    print(f"[i] <article> tags: {counts['article']}")
    print(f"[i] <details> (accordion) tags: {counts['details_accordion']}")
    print(f"[i] Elements with dealer-like class/id hints: {counts['dealer_hint_elements']}")

    if counts["dealer_hint_breakdown"]:
        print("[i] Hint breakdown (substring -> element count):")
        for hint, count in sorted(counts["dealer_hint_breakdown"].items(), key=lambda x: -x[1]):
            print(f"    {hint!r}: {count}")

    fa_keywords = ("نمایندگی", "تلفن", "آدرس", "مراکز", "فروش", "خدمات")
    print("\n[i] Persian keyword frequency in raw HTML:")
    for kw in fa_keywords:
        print(f"    {kw!r}: {raw_html.count(kw)}")

    _print_section("Scripts & embedded data")
    _scan_scripts(soup, raw_html)


def probe() -> int:
    response = _fetch_page()
    html = response.text
    _save_raw_html(html)

    soup = BeautifulSoup(html, "html.parser")
    _print_structure_summary(soup, html)

    print("\n[+] Probe complete.")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
