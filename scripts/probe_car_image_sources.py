#!/usr/bin/env python3
"""
Probe: technical crawlability of SeenAd car-gallery image sources.

Reads product-team spreadsheet rows and checks:
  - HTTP access + anti-bot markers
  - robots.txt Disallow for listing path
  - gallery-like <img> / og:image / JSON-LD image
  - sample image Content-Type / bytes / dimensions
  - watermark hints in URL/alt/filename

Usage:
  python scripts/probe_car_image_sources.py
  python scripts/probe_car_image_sources.py --limit 3 -v
  docker compose exec -T backend python3 scripts/probe_car_image_sources.py

Output:
  data/car_image_sources_probe.json
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, SSLError, Timeout

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_PATH = Path("data/car_image_sources_probe.json")
REQUEST_TIMEOUT_S = 25.0

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
}

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "attention required",
    "access denied",
    "captcha",
    "bot detection",
)

WATERMARK_HINTS = (
    "watermark",
    "واترمارک",
    "واتر مارک",
    "copyright",
    "کپی رایت",
    "logo-overlay",
    "wm_",
    "_wm",
)

SKIP_IMG_RE = re.compile(
    r"(logo|icon|sprite|favicon|avatar|pixel|1x1|blank|placeholder|"
    r"loading|spinner|banner-ad|advert)",
    re.I,
)
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|avif|gif|bmp)(\?|$)", re.I)
GALLERY_HINT_RE = re.compile(
    r"(gallery|galleries|product|products|models|carspecs|car-image|"
    r"images/cars|cdn\.(khodrobank|car\.ir)|uploads|media)",
    re.I,
)

SOURCES = [
    {
        "id": 1,
        "name": "ایران‌خودرو",
        "kind": "official_oem",
        "url": "https://www.ikco.ir/fa/ProductList.aspx",
        "follow_selectors": ["a[href*='Product']", "a[href*='product']"],
    },
    {
        "id": 2,
        "name": "مدیران خودرو",
        "kind": "official_oem",
        "url": "https://www.mvmco.ir/",
        "follow_selectors": [
            "a[href*='product']",
            "a[href*='models']",
            "a[href*='mvm']",
            "a[href*='fownix']",
            "a[href*='exeed']",
        ],
    },
    {
        "id": 3,
        "name": "کرمان موتور",
        "kind": "official_oem",
        "url": "https://kermanmotor.com/products",
        "follow_selectors": ["a[href*='/products/']", "a[href*='product']"],
    },
    {
        "id": 4,
        "name": "گروه بهمن",
        "kind": "official_oem",
        "url": "https://bahman.ir/",
        "follow_selectors": ["a[href*='product']", "a[href*='car']", "a[href*='model']"],
    },
    {
        "id": 5,
        "name": "فردا موتورز",
        "kind": "official_oem",
        "url": "https://www.fardamotors.com/",
        "follow_selectors": ["a[href*='product']", "a[href*='model']", "a[href*='car']"],
    },
    {
        "id": 6,
        "name": "ماموت خودرو",
        "kind": "official_oem",
        "url": "https://mammutkhodro.com/models/",
        "follow_selectors": ["a[href*='/models/']", "a[href*='product']"],
    },
    {
        "id": 7,
        "name": "لاماری",
        "kind": "official_oem",
        "url": "https://lamari.ir/",
        "follow_selectors": ["a[href*='product']", "a[href*='eama']", "a[href*='model']"],
    },
    {
        "id": 8,
        "name": "سایپا",
        "kind": "official_oem",
        "url": "https://www.saipacorp.com/",
        "follow_selectors": ["a[href*='product']", "a[href*='car']", "a[href*='model']"],
    },
    {
        "id": 9,
        "name": "Car.ir",
        "kind": "media",
        "url": "https://car.ir/galleries",
        "follow_selectors": ["a[href*='galler']", "a[href*='/cars/']"],
    },
    {
        "id": 10,
        "name": "خودروبانک",
        "kind": "media",
        "url": "https://www.khodrobank.com/cars",
        "follow_selectors": ["a.carspecs__item", "a[href*='/cars/']"],
        "extra_urls": [
            "https://www.khodrobank.com/cars/BAW/821/BAW-212-Navigator",
        ],
    },
    {
        "id": 11,
        "name": "EVOX Images",
        "kind": "commercial_api",
        "url": "https://evoximages.com/evox-api-ec/",
        "follow_selectors": [],
    },
    {
        "id": 12,
        "name": "IMAGIN.studio",
        "kind": "commercial_api",
        "url": "https://www.imaginstudio.com/solutions/api",
        "follow_selectors": [],
    },
    {
        "id": 13,
        "name": "Wikimedia Commons",
        "kind": "open_license",
        "url": "https://commons.wikimedia.org/w/api.php?action=query&list=search&srsearch=Peugeot%20405%20Iran&srnamespace=6&format=json",
        "follow_selectors": [],
        "is_json": True,
    },
    {
        "id": 14,
        "name": "NetCarShow",
        "kind": "international_archive",
        "url": "https://www.netcarshow.com/",
        "follow_selectors": ["a[href$='/']"],
    },
    {
        "id": 15,
        "name": "Caricos",
        "kind": "international_archive",
        "url": "https://www.caricos.com/",
        "follow_selectors": ["a[href*='/cars/']", "a[href*='gallery']"],
    },
    {
        "id": 16,
        "name": "باما",
        "kind": "ugc_classifieds",
        "url": "https://bama.ir/car",
        "follow_selectors": ["a[href*='/car/']", "a[href*='detail']"],
    },
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _anti_bot(status: int, text: str) -> dict[str, Any]:
    lowered = (text or "")[:80000].lower()
    hits = [m for m in ANTI_BOT_MARKERS if m in lowered]
    likely = status in (403, 429, 503) or bool(hits)
    if status == 200 and "just a moment" not in lowered and "challenge-platform" not in lowered:
        likely = False if not hits else likely
    return {"status": status, "likely_blocked": likely, "markers": hits}


def _robots(session: requests.Session, page_url: str) -> dict[str, Any]:
    parsed = urlparse(page_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    path = parsed.path or "/"
    out: dict[str, Any] = {"url": robots_url, "status": None, "disallow_hits": []}
    try:
        r = session.get(robots_url, timeout=12, verify=False, allow_redirects=True)
        out["status"] = r.status_code
        if r.status_code != 200:
            return out
        text = r.text
        out["bytes"] = len(text)
        disallow = []
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("disallow:"):
                rule = line.split(":", 1)[1].strip()
                if rule and (path.startswith(rule) or rule == "/" or rule in path):
                    disallow.append(rule)
        out["disallow_hits"] = disallow[:12]
        out["has_sitemap"] = "sitemap:" in text.lower()
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _abs(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("data:") or href.startswith("javascript:"):
        return None
    if href.startswith("//"):
        href = "https:" + href
    return urljoin(base, href)


def _looks_like_photo(url: str) -> bool:
    if SKIP_IMG_RE.search(url):
        return False
    if IMG_EXT_RE.search(url) or GALLERY_HINT_RE.search(url):
        return True
    return False


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if not u or u in seen:
            return
        if not _looks_like_photo(u):
            return
        seen.add(u)
        urls.append(u)

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-full"):
            add(_abs(base_url, img.get(attr)))
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            add(_abs(base_url, first))

    for tag in soup.find_all(["source", "a", "div", "span"]):
        for attr in ("href", "data-src", "data-image", "data-bg", "data-background"):
            val = tag.get(attr)
            if val and IMG_EXT_RE.search(val):
                add(_abs(base_url, val))
        style = tag.get("style") or ""
        m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
        if m:
            add(_abs(base_url, m.group(1)))

    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in {"og:image", "og:image:url", "twitter:image"}:
            add(_abs(base_url, meta.get("content")))

    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = script.string or script.get_text() or ""
        for m in re.finditer(r"https?://[^\"'\\s]+\.(?:jpe?g|png|webp)", raw, re.I):
            add(m.group(0))

    return urls[:80]


def _follow_links(soup: BeautifulSoup, base_url: str, selectors: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for sel in selectors:
        for a in soup.select(sel):
            href = _abs(base_url, a.get("href"))
            if not href or href in seen:
                continue
            if urlparse(href).netloc != urlparse(base_url).netloc:
                continue
            if href.rstrip("/") == base_url.rstrip("/"):
                continue
            seen.add(href)
            found.append(href)
            if len(found) >= 8:
                return found
    return found


def _image_head(session: requests.Session, url: str) -> dict[str, Any]:
    info: dict[str, Any] = {"url": url}
    try:
        r = session.get(url, timeout=15, verify=False, stream=True)
        info["status"] = r.status_code
        info["content_type"] = r.headers.get("Content-Type")
        info["content_length_header"] = r.headers.get("Content-Length")
        chunk = next(r.iter_content(64 * 1024), b"")
        r.close()
        info["bytes_read"] = len(chunk)
        info["looks_image"] = bool(
            (info["content_type"] or "").startswith("image/") or chunk[:3] in (b"\xff\xd8\xff", b"PNG")
        )
        if chunk[:2] == b"\xff\xd8":
            info["format"] = "jpeg"
        elif chunk[:8] == b"\x89PNG\r\n\x1a\n":
            info["format"] = "png"
        elif chunk[:4] == b"RIFF" and b"WEBP" in chunk[:16]:
            info["format"] = "webp"
        wm = [h for h in WATERMARK_HINTS if h in url.lower()]
        info["watermark_url_hints"] = wm
        try:
            from PIL import Image

            im = Image.open(BytesIO(chunk))
            info["width"] = im.size[0]
            info["height"] = im.size[1]
        except Exception:
            pass
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _fetch(session: requests.Session, url: str, is_json: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url}
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT_S, verify=False, allow_redirects=True)
        out["status"] = r.status_code
        out["final_url"] = str(r.url)
        out["content_type"] = r.headers.get("Content-Type")
        out["bytes"] = len(r.content)
        text = r.text if not is_json else r.text
        out["anti_bot"] = _anti_bot(r.status_code, text)
        if is_json:
            try:
                payload = r.json()
                out["json_keys"] = list(payload.keys())[:20] if isinstance(payload, dict) else type(payload).__name__
                out["json_ok"] = True
            except Exception as exc:
                out["json_ok"] = False
                out["json_error"] = str(exc)
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("title")
        out["title"] = html_lib.unescape(title.get_text(" ", strip=True)) if title else None
        out["images"] = _extract_images(soup, str(r.url))
        out["image_count"] = len(out["images"])
        body_l = r.text.lower()
        out["watermark_page_hints"] = [h for h in WATERMARK_HINTS if h in body_l]
        return out
    except SSLError as exc:
        out["error"] = f"ssl: {exc}"
    except Timeout as exc:
        out["error"] = f"timeout: {exc}"
    except RequestException as exc:
        out["error"] = str(exc)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def probe_source(session: requests.Session, spec: dict[str, Any], verbose: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": spec["id"],
        "name": spec["name"],
        "kind": spec["kind"],
        "listing_url": spec["url"],
        "probed_at": _now_iso(),
    }
    if verbose:
        print(f"\n[{spec['id']:02d}] {spec['name']}  {spec['url']}")

    result["robots"] = _robots(session, spec["url"])
    listing = _fetch(session, spec["url"], is_json=bool(spec.get("is_json")))
    result["listing"] = listing

    pages: list[dict[str, Any]] = []
    extra = list(spec.get("extra_urls") or [])
    if listing.get("status") == 200 and not spec.get("is_json"):
        soup = BeautifulSoup(
            requests.get(
                spec["url"],
                headers=BROWSER_HEADERS,
                timeout=REQUEST_TIMEOUT_S,
                verify=False,
            ).text
            if False
            else "",
            "html.parser",
        )
        # Re-parse from listing fetch by re-requesting only if we have HTML.
        try:
            r = session.get(spec["url"], timeout=REQUEST_TIMEOUT_S, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
            extra.extend(_follow_links(soup, str(r.url), spec.get("follow_selectors") or []))
        except Exception:
            pass

    for href in extra[:3]:
        pages.append(_fetch(session, href))
    result["sample_pages"] = pages

    candidate_imgs: list[str] = []
    for blob in [listing, *pages]:
        candidate_imgs.extend(blob.get("images") or [])
    # Prefer gallery-looking URLs
    candidate_imgs.sort(key=lambda u: (0 if GALLERY_HINT_RE.search(u) else 1, len(u)))
    uniq: list[str] = []
    seen: set[str] = set()
    for u in candidate_imgs:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
        if len(uniq) >= 3:
            break
    result["sample_image_heads"] = [_image_head(session, u) for u in uniq]

    status = listing.get("status")
    blocked = bool((listing.get("anti_bot") or {}).get("likely_blocked"))
    img_ok = any(h.get("looks_image") for h in result["sample_image_heads"])
    has_imgs = (listing.get("image_count") or 0) > 0 or any(
        (p.get("image_count") or 0) > 0 for p in pages
    )
    result["technical"] = {
        "http_ok": status == 200 and not blocked,
        "blocked": blocked,
        "has_candidate_images": bool(has_imgs),
        "sample_image_downloadable": img_ok,
        "robots_blocks_path": bool(result["robots"].get("disallow_hits")),
        "error": listing.get("error"),
    }
    if verbose:
        t = result["technical"]
        print(
            f"    http_ok={t['http_ok']} images={has_imgs} "
            f"downloadable={img_ok} robots_block={t['robots_blocks_path']} "
            f"err={t['error']}"
        )
    return result


def classify(row: dict[str, Any]) -> dict[str, Any]:
    """Combine technical probe with license/quality policy from the spreadsheet."""
    kind = row["kind"]
    tech = row["technical"]
    http_ok = tech["http_ok"]
    imgs = tech["has_candidate_images"] or tech["sample_image_downloadable"]

    if kind == "ugc_classifieds":
        decision = "no_go"
        reason = "تصاویر UGC آگهی؛ کیفیت و مجوز مناسب گالری محصول نیست."
    elif kind == "international_archive":
        decision = "no_go_direct"
        reason = "از نظر فنی ممکن است باز باشد ولی مجوز تجاری پیش‌فرض ندارد."
    elif kind == "commercial_api":
        decision = "poc_paid"
        reason = "API تجاری؛ کراول عمومی ندارد، نیاز به Demo/قرارداد."
    elif kind == "media":
        decision = "conditional_license"
        reason = "پوشش بالا و از نظر فنی قابل کراول است؛ بازنشر بدون قرارداد توصیه نمی‌شود."
    elif kind == "open_license":
        decision = "gap_fill"
        reason = "API باز است؛ پوشش نامنظم و باید لایسنس هر فایل ذخیره شود."
    elif kind == "official_oem":
        if http_ok and imgs:
            decision = "preferred_probe_ok"
            reason = "منبع رسمی بازار ایران؛ از نظر فنی تصویر قابل استخراج دیده شد."
        elif http_ok:
            decision = "preferred_needs_deeper_probe"
            reason = "سایت باز است ولی گالری محصول در صفحه اول واضح نبود."
        else:
            decision = "preferred_blocked_or_down"
            reason = "منبع رسمی است ولی الان HTTP/آنتی‌بات مانع شد."
    else:
        decision = "unknown"
        reason = ""

    return {"decision": decision, "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe car gallery image sources")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    sources = SOURCES[: args.limit] if args.limit else SOURCES
    session = _session()
    rows = []
    for spec in sources:
        row = probe_source(session, spec, args.verbose)
        row["classification"] = classify(row)
        rows.append(row)

    summary = {
        "probed_at": _now_iso(),
        "count": len(rows),
        "by_decision": {},
    }
    for row in rows:
        d = row["classification"]["decision"]
        summary["by_decision"][d] = summary["by_decision"].get(d, 0) + 1

    payload = {"summary": summary, "sources": rows}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}  ({len(rows)} sources)")
    for row in rows:
        t = row["technical"]
        c = row["classification"]
        print(
            f"{row['id']:2d}. {row['name']:18s}  {c['decision']:28s}  "
            f"http={t['http_ok']} imgs={t['has_candidate_images']} "
            f"dl={t['sample_image_downloadable']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
