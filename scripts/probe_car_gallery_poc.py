#!/usr/bin/env python3
"""
POC probe: official OEM car-gallery extraction for SeenAd.

Smoke-crawls 2–3 product pages per approved source and records gallery
image URLs + a few inspected dimensions. Does not bulk-download binaries.

Sources (step-1 decision):
  kermanmotor, mvm, fownix, bahman, lamari, mammut, saipa, ikco

Usage:
  python scripts/probe_car_gallery_poc.py
  python scripts/probe_car_gallery_poc.py --models 3 --inspect-images 3 -v
  python scripts/probe_car_gallery_poc.py --source kermanmotor --models 2
  docker exec -i seenad_backend python3 scripts/probe_car_gallery_poc.py -v

Output:
  data/car_gallery_poc_smoke.json
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import random
import re
import struct
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException, SSLError, Timeout

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_PATH = Path("data/car_gallery_poc_smoke.json")
REQUEST_TIMEOUT_S = 35.0
DELAY_MIN_S = 0.7
DELAY_MAX_S = 1.6

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "request rejected",
    "access denied",
    "attention required",
)

SKIP_NAME_RE = re.compile(
    r"(logo|favicon|icon|sprite|btn_|button|arrow|motto|placeholder|"
    r"vector-smart-object|close\.png|open\.png|left\.png|right\.png|"
    r"menuDown|navigate-arrow|webresource\.axd)",
    re.I,
)
WP_SIZE_RE = re.compile(r"-(?:\d+x\d+|scaled)(?=\.(?:jpe?g|png|webp|avif)$)", re.I)
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|avif)(\?|$)", re.I)
DOWNLOAD_RE = re.compile(r"/Download\.aspx\?id=[^\"'\s]+", re.I)
SITEFILE_RE = re.compile(
    r"https://sitefile\.bahman\.ir/(?:thumb/)?([a-f0-9-]{36})(?:/(\d+))?",
    re.I,
)
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value).replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def abs_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("data:", "javascript:", "mailto:")):
        return None
    if href.startswith("//"):
        href = "https:" + href
    return urljoin(base, href)


def strip_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def wp_canonical(url: str) -> str:
    path = strip_query(url)
    return WP_SIZE_RE.sub("", path)


def looks_skipped(url: str) -> bool:
    return bool(SKIP_NAME_RE.search(url))


def unique_keep_order(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(w), int(h)
        if marker in (0xD8, 0xD9) or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= len(data):
            return None
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + length
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def image_dims(data: bytes) -> tuple[int, int] | None:
    return jpeg_size(data) or png_size(data)


class HttpClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.session.verify = False

    def sleep(self) -> None:
        time.sleep(random.uniform(DELAY_MIN_S, DELAY_MAX_S))

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_S)
        kwargs.setdefault("allow_redirects", True)
        return self.session.get(url, **kwargs)

    def fetch_html(self, url: str, referer: str | None = None) -> dict[str, Any]:
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            response = self.get(url, headers=headers)
        except (SSLError, Timeout, RequestException) as exc:
            return {"url": url, "error": str(exc), "status": None, "html": "", "soup": None}
        text = response.text or ""
        lowered = text[:80000].lower()
        blocked = any(m in lowered for m in ANTI_BOT_MARKERS) or response.status_code in (403, 429)
        soup = BeautifulSoup(text, "html.parser") if response.status_code == 200 else None
        title = None
        if soup and soup.title:
            title = clean_text(soup.title.get_text(" ", strip=True))
        return {
            "url": str(response.url),
            "requested_url": url,
            "status": response.status_code,
            "bytes": len(response.content),
            "blocked": blocked,
            "title": title,
            "html": text,
            "soup": soup,
            "error": None,
        }

    def inspect_image(self, url: str, referer: str | None = None) -> dict[str, Any]:
        headers = {}
        if referer:
            headers["Referer"] = referer
        info: dict[str, Any] = {"url": url}
        try:
            response = self.get(url, headers=headers, stream=True)
            info["status"] = response.status_code
            info["content_type"] = response.headers.get("Content-Type")
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                info["content_length"] = int(length)
            chunk = next(response.iter_content(96 * 1024), b"")
            response.close()
            info["bytes_read"] = len(chunk)
            dims = image_dims(chunk)
            if dims:
                info["width"], info["height"] = dims
            ctype = (info.get("content_type") or "").lower()
            info["looks_image"] = ctype.startswith("image/") or bool(dims)
            if chunk[:3] == b"\xff\xd8\xff":
                info["format"] = "jpeg"
            elif chunk[:8] == b"\x89PNG\r\n\x1a\n":
                info["format"] = "png"
            elif chunk[:4] == b"RIFF" and b"WEBP" in chunk[:16]:
                info["format"] = "webp"
        except Exception as exc:
            info["error"] = str(exc)
        return info


def listing_item(name: str | None, url: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"name": name, "url": url}
    if extra:
        row.update(extra)
    return row


def same_host(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


# ---------------------------------------------------------------------------
# Per-source listing + gallery extractors
# ---------------------------------------------------------------------------


def kerman_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw = (anchor.get("href") or "").strip()
        if "products/" not in raw or "product-dev" in raw:
            continue
        href = abs_url(page_url, raw)
        if not href or href in seen:
            continue
        path = urlparse(href).path.rstrip("/")
        if path.endswith("/products") or path.count("/") < 2:
            continue
        seen.add(href)
        name = clean_text(anchor.get_text(" ", strip=True))
        items.append(listing_item(name, href.split("#")[0]))
    return items


def kerman_gallery(soup: BeautifulSoup, page_url: str, html: str) -> list[dict[str, Any]]:
    folders: list[str] = []
    for match in re.finditer(r"/images/product/([^/]+)/", html, re.I):
        folders.append(match.group(1))
    if not folders:
        return []
    primary = Counter(folders).most_common(1)[0][0]
    urls: list[str] = []
    for match in re.finditer(
        rf"https?://kermanmotor\.com/images/product/{re.escape(primary)}/[^\"'\s?]+\.(?:jpe?g|png|webp)",
        html,
        re.I,
    ):
        urls.append(match.group(0).split("?")[0])
    # also relative
    for match in re.finditer(
        rf"/images/product/{re.escape(primary)}/[^\"'\s?]+\.(?:jpe?g|png|webp)",
        html,
        re.I,
    ):
        absed = abs_url(page_url, match.group(0).split("?")[0])
        if absed:
            urls.append(absed)
    out = []
    for url in unique_keep_order(urls):
        if "/colors/" in url.lower():
            kind = "color_swatch"
        elif re.search(r"/gal-\d+", url, re.I):
            kind = "gallery"
        elif re.search(r"/G\d+-", url):
            kind = "gallery"
        else:
            kind = "product"
        if looks_skipped(url):
            continue
        out.append({"url": url, "kind": kind, "folder": primary})
    return out


def mvm_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw = (anchor.get("href") or "").strip()
        if "products/" not in raw:
            continue
        href = abs_url(page_url, raw)
        if not href or href in seen:
            continue
        host = urlparse(href).netloc.lower()
        if host not in {"www.mvmco.ir", "mvmco.ir"}:
            continue
        path = urlparse(href).path.rstrip("/")
        if path.count("/") < 2:
            continue
        canon = href.split("#")[0]
        seen.add(canon)
        slug = path.rsplit("/", 1)[-1]
        name = clean_text(anchor.get_text(" ", strip=True)) or slug
        if name in {"نمایش مشخصات", "مشاهده"}:
            name = slug
        items.append(listing_item(name, canon, {"slug": slug}))
    return items


def mvm_like_gallery(soup: BeautifulSoup, page_url: str, html: str, slug: str | None) -> list[dict[str, Any]]:
    allow_dirs = ("/uploads/sliders/", "/uploads/banners/", "/uploads/hotspots/",
                  "/uploads/attributes/", "/uploads/selling-points/")
    urls: list[tuple[str, str]] = []
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src"):
            href = abs_url(page_url, img.get(attr))
            if not href or looks_skipped(href):
                continue
            path = urlparse(href).path
            if any(d in path for d in allow_dirs):
                kind = path.split("/uploads/")[1].split("/")[0] if "/uploads/" in path else "product"
                urls.append((href.split("?")[0], kind))
            elif slug and "/uploads/cars/" in path and slug.lower() in path.lower():
                urls.append((href.split("?")[0], "hero"))
    return [{"url": u, "kind": k} for u, k in unique_keep_order_pairs(urls)]


def unique_keep_order_pairs(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, kind in rows:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, kind))
    return out


def fownix_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='/product/']"):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen or not same_host(page_url, href):
            continue
        path = urlparse(href).path.rstrip("/")
        if path.count("/") < 2:
            continue
        seen.add(href)
        slug = path.rsplit("/", 1)[-1]
        name = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True)) or slug
        items.append(listing_item(name, href.split("#")[0], {"slug": slug}))
    return items


def bahman_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='/product/']"):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen:
            continue
        if not re.search(r"/product/\d+/?$", urlparse(href).path):
            continue
        seen.add(href)
        name = clean_text(anchor.get_text(" ", strip=True))
        items.append(listing_item(name, href.split("#")[0]))
    return items


def bahman_gallery(soup: BeautifulSoup, page_url: str, html: str) -> list[dict[str, Any]]:
    section = soup.select_one("section.pgallery, #section-gallery")
    if section is None:
        return []
    scope = str(section)
    best: dict[str, tuple[int, str]] = {}
    for match in SITEFILE_RE.finditer(scope):
        uuid, size_s = match.group(1), match.group(2)
        size = int(size_s) if size_s else 9999
        url = match.group(0)
        if uuid not in best or size > best[uuid][0]:
            if size_s:
                url = f"https://sitefile.bahman.ir/thumb/{uuid}/{size}"
            else:
                url = f"https://sitefile.bahman.ir/{uuid}"
            best[uuid] = (size, url)
    color_section = soup.select_one("section.color-gallery")
    color_ids: set[str] = set()
    if color_section:
        for match in SITEFILE_RE.finditer(str(color_section)):
            color_ids.add(match.group(1))
    out = []
    for uuid, (size, url) in best.items():
        kind = "color" if uuid in color_ids else "gallery"
        out.append({"url": url, "kind": kind, "asset_id": uuid, "size_hint": size if size != 9999 else None})
    # Prefer original/full when present in page (no /thumb/)
    originals = set(re.findall(r"https://sitefile\.bahman\.ir/([a-f0-9-]{36})(?![/\w])", scope, re.I))
    for row in out:
        if row["asset_id"] in originals:
            row["url"] = f"https://sitefile.bahman.ir/{row['asset_id']}"
            row["size_hint"] = None
    return out


def lamari_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='/cars/']"):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen or not same_host(page_url, href):
            continue
        path = urlparse(href).path.rstrip("/")
        if path.count("/") < 2 or "/cars/page/" in path:
            continue
        seen.add(href)
        name = clean_text(anchor.get_text(" ", strip=True)) or path.rsplit("/", 1)[-1]
        if len(name or "") < 2:
            continue
        items.append(listing_item(name, href.split("#")[0]))
    return items


def wordpress_gallery(soup: BeautifulSoup, page_url: str, html: str) -> list[dict[str, Any]]:
    raw: list[str] = []
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-full-url"):
            href = abs_url(page_url, img.get(attr))
            if href and IMG_EXT_RE.search(href) and "/wp-content/uploads/" in href:
                raw.append(href.split("?")[0])
        srcset = img.get("srcset") or ""
        if srcset:
            last = srcset.split(",")[-1].strip().split(" ")[0]
            href = abs_url(page_url, last)
            if href and "/wp-content/uploads/" in href:
                raw.append(href.split("?")[0])
    # fancybox / gallery anchors
    for anchor in soup.find_all("a", href=True):
        href = abs_url(page_url, anchor.get("href"))
        if href and IMG_EXT_RE.search(href) and "/wp-content/uploads/" in href:
            raw.append(href.split("?")[0])
    canonical: dict[str, str] = {}
    for url in raw:
        if looks_skipped(url):
            continue
        key = wp_canonical(url)
        current = canonical.get(key)
        if current is None:
            canonical[key] = url
            continue
        # prefer unsized original over -300x200 / -scaled
        if WP_SIZE_RE.search(current) and not WP_SIZE_RE.search(url):
            canonical[key] = url
        elif "-scaled" in current and not WP_SIZE_RE.search(url):
            canonical[key] = url
    return [{"url": u, "kind": "gallery"} for u in canonical.values()]


def mammut_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='/models/']"):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen:
            continue
        path = urlparse(href).path.rstrip("/")
        if path.rstrip("/") in {"/models", ""} or path.count("/") < 2:
            continue
        seen.add(href)
        slug = path.rsplit("/", 1)[-1]
        name = clean_text(anchor.get_text(" ", strip=True)) or slug
        if name.lower() in {"skip to content", "منو"}:
            continue
        items.append(listing_item(name, href.split("#")[0], {"slug": slug}))
    return items


def saipa_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen:
            continue
        if not re.search(r"/fa/products/producing/[^/]+/[^/#]+/?$", urlparse(href).path):
            continue
        seen.add(href)
        path = urlparse(href).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        brand = path.rsplit("/", 2)[-2]
        name = clean_text(anchor.get("title") or slug)
        items.append(listing_item(name, href.split("#")[0], {"brand": brand, "slug": slug}))
    return items


def saipa_gallery(soup: BeautifulSoup, page_url: str, html: str) -> list[dict[str, Any]]:
    slug = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    urls: list[str] = []
    for img in soup.find_all("img"):
        href = abs_url(page_url, img.get("src"))
        if not href or looks_skipped(href):
            continue
        path = urlparse(href).path
        if "/Products/All/" not in path and "/Products/" not in path:
            continue
        if "/Thumbnails/" in path:
            continue
        urls.append(href.split("?")[0])
    # Prefer images whose folder mentions the model slug
    slug_l = slug.lower().replace("-", "")
    filtered = [u for u in urls if slug_l[:4] in u.lower().replace("-", "").replace("_", "")]
    keep = filtered or urls
    return [{"url": u, "kind": "gallery"} for u in unique_keep_order(keep)]


def ikco_listing(client: HttpClient, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='Product.aspx']"):
        href = abs_url(page_url, anchor.get("href"))
        if not href or href in seen:
            continue
        if "Product.aspx" not in href:
            continue
        seen.add(href)
        name = clean_text(anchor.get_text(" ", strip=True))
        if name:
            name = name.lstrip("• ").strip()
        parsed = urlparse(href)
        q = parse_qs(parsed.query)
        product_id = (q.get("ID") or [None])[0]
        items.append(listing_item(name, href, {"product_id": product_id}))
    return items


def ikco_gallery_url(product_url: str) -> str:
    parsed = urlparse(product_url)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["Section"] = ["4"]
    query = urlencode({key: values[0] for key, values in q.items()})
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def ikco_gallery(soup: BeautifulSoup, page_url: str, html: str) -> list[dict[str, Any]]:
    panel = soup.find(id=re.compile(r"PhotoGallery_UpdatePanel1"))
    scope = str(panel) if panel else html
    urls = unique_keep_order(DOWNLOAD_RE.findall(scope))
    out = []
    for rel in urls:
        absed = abs_url(page_url, rel)
        if not absed:
            continue
        out.append({"url": absed, "kind": "gallery", "handler": "Download.aspx"})
    interior = bool(soup.find(id=re.compile(r"PhotoGallery_btnInterior")))
    if interior and out:
        out[0]["notes"] = "interior_tab_present"
    return out


SOURCE_SPECS: list[dict[str, Any]] = [
    {
        "id": "kermanmotor",
        "name": "کرمان موتور",
        "listing_url": "https://kermanmotor.com/products",
        "listing": kerman_listing,
        "gallery": kerman_gallery,
    },
    {
        "id": "mvm",
        "name": "مدیران خودرو",
        "listing_url": "https://www.mvmco.ir/",
        "listing": mvm_listing,
        "gallery": lambda soup, url, html: mvm_like_gallery(
            soup, url, html, urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        ),
    },
    {
        "id": "fownix",
        "name": "فونیکس",
        "listing_url": "https://www.fownix.com/",
        "listing": fownix_listing,
        "gallery": lambda soup, url, html: mvm_like_gallery(
            soup, url, html, urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        ),
    },
    {
        "id": "bahman",
        "name": "گروه بهمن",
        "listing_url": "https://bahman.ir/",
        "listing": bahman_listing,
        "gallery": bahman_gallery,
    },
    {
        "id": "lamari",
        "name": "لاماری",
        "listing_url": "https://lamari.ir/",
        "listing": lamari_listing,
        "gallery": wordpress_gallery,
    },
    {
        "id": "mammut",
        "name": "ماموت خودرو",
        "listing_url": "https://mammutkhodro.com/models/",
        "listing": mammut_listing,
        "gallery": wordpress_gallery,
    },
    {
        "id": "saipa",
        "name": "سایپا",
        "listing_url": "https://www.saipacorp.com/fa/products/producing",
        "listing": saipa_listing,
        "gallery": saipa_gallery,
    },
    {
        "id": "ikco",
        "name": "ایران‌خودرو",
        "listing_url": "https://www.ikco.ir/fa/ProductList.aspx",
        "listing": ikco_listing,
        "gallery": ikco_gallery,
        "rewrite_detail": ikco_gallery_url,
    },
]


def probe_source(
    client: HttpClient,
    spec: dict[str, Any],
    *,
    models_limit: int,
    inspect_limit: int,
    verbose: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": spec["id"],
        "name": spec["name"],
        "listing_url": spec["listing_url"],
        "probed_at": _now_iso(),
    }
    if verbose:
        print(f"\n=== {spec['name']} ({spec['id']}) ===")
    listing = client.fetch_html(spec["listing_url"])
    result["listing_status"] = listing.get("status")
    result["listing_blocked"] = listing.get("blocked")
    result["listing_title"] = listing.get("title")
    result["listing_error"] = listing.get("error")
    if listing.get("error") or listing.get("blocked") or listing.get("status") != 200 or listing.get("soup") is None:
        result["ok"] = False
        result["models"] = []
        result["note"] = "listing fetch failed"
        if verbose:
            print("  listing FAILED", listing.get("status"), listing.get("error"), listing.get("blocked"))
        return result

    models = spec["listing"](client, listing["soup"], listing["url"])
    result["listing_model_count"] = len(models)
    picked = models[:models_limit]
    if verbose:
        print(f"  listing models={len(models)} picked={len(picked)}")

    rows: list[dict[str, Any]] = []
    for item in picked:
        client.sleep()
        detail_url = spec["rewrite_detail"](item["url"]) if spec.get("rewrite_detail") else item["url"]
        page = client.fetch_html(detail_url, referer=spec["listing_url"])
        row: dict[str, Any] = {
            "name": item.get("name"),
            "listing_url": item["url"],
            "detail_url": page.get("url") or detail_url,
            "status": page.get("status"),
            "blocked": page.get("blocked"),
            "title": page.get("title"),
            "error": page.get("error"),
        }
        if page.get("soup") is None or page.get("status") != 200 or page.get("blocked"):
            row["ok"] = False
            row["images"] = []
            rows.append(row)
            if verbose:
                print("  FAIL", item.get("name"), page.get("status"), page.get("blocked"))
            continue
        images = spec["gallery"](page["soup"], page["url"], page["html"])
        row["image_count"] = len(images)
        row["images"] = images
        inspect_targets = [img["url"] for img in images[:inspect_limit]]
        inspected = []
        for img_url in inspect_targets:
            client.sleep()
            inspected.append(client.inspect_image(img_url, referer=page["url"]))
        row["inspected"] = inspected
        widths = [i.get("width") for i in inspected if i.get("width")]
        row["sample_max_width"] = max(widths) if widths else None
        row["ok"] = bool(images)
        rows.append(row)
        if verbose:
            print(
                f"  {item.get('name')}: images={len(images)} "
                f"sample_w={row['sample_max_width']} status={page.get('status')}"
            )
    result["models"] = rows
    result["ok"] = any(r.get("ok") for r in rows)
    result["total_images"] = sum(r.get("image_count") or 0 for r in rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="POC probe official car galleries")
    parser.add_argument("--models", type=int, default=3, help="models per source")
    parser.add_argument("--inspect-images", type=int, default=3, help="images to HEAD/sample per model")
    parser.add_argument("--source", action="append", help="limit to source id (repeatable)")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    wanted = set(args.source or [])
    specs = [s for s in SOURCE_SPECS if not wanted or s["id"] in wanted]
    if wanted and not specs:
        print("unknown --source; valid:", ", ".join(s["id"] for s in SOURCE_SPECS), file=sys.stderr)
        return 2

    client = HttpClient()
    sources = []
    for spec in specs:
        sources.append(
            probe_source(
                client,
                spec,
                models_limit=args.models,
                inspect_limit=args.inspect_images,
                verbose=args.verbose,
            )
        )

    payload = {
        "probed_at": _now_iso(),
        "models_per_source": args.models,
        "inspect_images": args.inspect_images,
        "summary": [
            {
                "id": row["id"],
                "name": row["name"],
                "ok": row.get("ok"),
                "listing_models": row.get("listing_model_count"),
                "sampled_models": len(row.get("models") or []),
                "total_images": row.get("total_images"),
                "listing_status": row.get("listing_status"),
            }
            for row in sources
        ],
        "sources": sources,
    }
    # soups are not serializable; already not stored in result
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    for row in payload["summary"]:
        print(
            f"{row['id']:14s} ok={row['ok']!s:5s} listing={row['listing_models']} "
            f"sampled={row['sampled_models']} images={row['total_images']}"
        )
    return 0 if all(r.get("ok") for r in sources) else 1


if __name__ == "__main__":
    sys.exit(main())
