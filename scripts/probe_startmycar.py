#!/usr/bin/env python3
"""
Probe: startmycar.com catalog starting at /pickmake.

Maps brand listing → model pages → hub + trims / fusebox / manuals.

Findings this script re-validates:
  - /pickmake is SSR HTML (jQuery), ~89 brands via a[href$='/pickmodel']
  - Brand pages list models via a[href^='/{brand}/'] (no public JSON API)
  - /{brand}/{model}/trims exists but is empty ("We don't have information")
  - Engine "specs" guide is a generic educational article, not numeric specs
  - Fuse-box year pages embed window.fuseboxesData (fuse table + diagram images)
  - Owner manuals expose direct PDF links on manuals.startmycar.com
  - robots.txt allows catalog/info paths; member profiles are disallowed
  - #google_vignette is an AdSense overlay, not a real page section

Usage:
  python scripts/probe_startmycar.py
  python scripts/probe_startmycar.py --limit-brands 2 --sample-model toyota/camry -v
  docker compose exec -T backend python3 scripts/probe_startmycar.py

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

BASE_URL = "https://www.startmycar.com"
LISTING_URL = f"{BASE_URL}/pickmake"
OUTPUT_PATH = Path("data/startmycar_probe_sample.json")
RAW_LISTING_PATH = Path("data/startmycar_probe_raw.html")
REQUEST_TIMEOUT_S = 45.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,fa-IR;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
    "arvancloud",
)
IRAN_BRAND_HINTS = (
    "saipa",
    "ikco",
    "iran-khodro",
    "pride",
    "mvm",
    "bahman",
    "kerman",
    "modiran",
)
WS_RE = re.compile(r"\s+")
BRAND_HREF_RE = re.compile(r"^/([a-z0-9-]+)/pickmodel/?$", re.IGNORECASE)
MODEL_HREF_RE = re.compile(r"^/([a-z0-9-]+)/([a-z0-9-]+)/?$", re.IGNORECASE)
SKIP_MODEL_SLUGS = {"pickmodel", "pickmake", "problems", "reviews", "guides", "compare", "community"}
FUSEBOX_YEAR_RE = re.compile(r"/info/fusebox/(\d{4})(?:/([^/?#]+))?", re.IGNORECASE)
PDF_HREF_RE = re.compile(r"\.pdf($|\?)", re.IGNORECASE)
def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = WS_RE.sub(" ", text).strip()
    return text or None


def detect_anti_bot(html: str) -> list[str]:
    lowered = html.lower()
    return [m for m in ANTI_BOT_MARKERS if m in lowered]


def parse_window_json(html: str, name: str) -> Any:
    needle = f"window.{name}"
    idx = html.find(needle)
    if idx < 0:
        return None
    eq = html.find("=", idx)
    if eq < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(html[eq + 1 :].lstrip())
        return obj
    except json.JSONDecodeError:
        return None


def fetch(session: requests.Session, url: str) -> tuple[int, str, str]:
    response = session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    response.encoding = response.encoding or "utf-8"
    return response.status_code, response.text, response.url


def extract_brands(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href$='/pickmodel']"):
        href = anchor.get("href") or ""
        match = BRAND_HREF_RE.search(urlparse(href).path)
        if not match:
            continue
        slug = match.group(1).lower()
        if slug in seen:
            continue
        seen.add(slug)
        img = anchor.select_one("img")
        name = clean_text(img.get("alt") or img.get("title") if img else None)
        if not name:
            name = clean_text(anchor.get_text(" ", strip=True)) or slug.replace("-", " ").title()
        brands.append(
            {
                "name": name,
                "slug": slug,
                "url": urljoin(BASE_URL, href),
                "logo": urljoin(BASE_URL, img["src"]) if img and img.get("src") else None,
            }
        )
    return brands


def extract_models(html: str, brand_slug: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    prefix = f"/{brand_slug}/"
    for anchor in soup.find_all("a", href=True):
        path = urlparse(anchor["href"]).path.rstrip("/")
        match = MODEL_HREF_RE.match(path)
        if not match:
            continue
        if match.group(1).lower() != brand_slug:
            continue
        model_slug = match.group(2).lower()
        if model_slug in SKIP_MODEL_SLUGS:
            continue
        url = urljoin(BASE_URL, path)
        if url in seen:
            continue
        seen.add(url)
        models.append(
            {
                "brand_slug": brand_slug,
                "model_slug": model_slug,
                "name": clean_text(anchor.get_text(" ", strip=True)) or model_slug.replace("-", " ").title(),
                "url": url,
            }
        )
    return models


def extract_hub_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    wanted = {
        "reviews": "/reviews",
        "manuals": "/info/manuals",
        "service_manuals": "/info/manuals/service-repair",
        "guides": "/guides",
        "fusebox": "/info/fusebox",
        "compare": "/compare",
        "problems": "/problems",
        "trims": "/trims",
    }
    found: dict[str, str] = {}
    hrefs = [a.get("href") or "" for a in soup.find_all("a", href=True)]
    for key, suffix in wanted.items():
        for href in hrefs:
            path = urlparse(href).path.rstrip("/")
            if path.endswith(suffix):
                found[key] = urljoin(BASE_URL, href)
                break
    return found


def extract_page_modelo(html: str) -> dict[str, Any] | None:
    obj = parse_window_json(html, "pageModelo")
    return obj if isinstance(obj, dict) else None


def probe_trims(session: requests.Session, model_path: str) -> dict[str, Any]:
    url = urljoin(BASE_URL, f"/{model_path}/trims")
    status, html, final = fetch(session, url)
    soup = BeautifulSoup(html, "html.parser")
    empty_msg = None
    p = soup.select_one("p.text-large")
    if p:
        empty_msg = clean_text(p.get_text(" ", strip=True))
    robots = soup.find("meta", attrs={"name": "robots"})
    return {
        "url": final,
        "status": status,
        "html_bytes": len(html),
        "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
        "h1": clean_text(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else None),
        "empty_message": empty_msg,
        "robots": robots.get("content") if robots else None,
        "has_trim_cards": bool(soup.select("[class*='TrimCard'], .ModelTrimSlim")),
        "looks_empty": bool(empty_msg and "don't have information" in empty_msg.lower()) or not empty_msg,
    }


def probe_engine_guide(session: requests.Session, model_path: str) -> dict[str, Any]:
    url = urljoin(BASE_URL, f"/{model_path}/guides/engine-technical-specifications")
    status, html, final = fetch(session, url)
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one(".Article, .js-article")
    text = clean_text(article.get_text(" ", strip=True) if article else None) or ""
    numeric_hints = sum(text.lower().count(w) for w in (" hp", " kw", " n.m", "rpm", "liter"))
    return {
        "url": final,
        "status": status,
        "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
        "article_chars": len(text),
        "has_spec_table": bool(soup.find("table")),
        "looks_generic_explainer": "what the most usual technical specifications" in text.lower()
        or "what do the technical specifications" in (soup.title.get_text() if soup.title else "").lower(),
        "numeric_hint_hits": numeric_hints,
    }


def probe_fusebox(session: requests.Session, model_path: str) -> dict[str, Any]:
    listing_url = urljoin(BASE_URL, f"/{model_path}/info/fusebox")
    status, html, final = fetch(session, listing_url)
    soup = BeautifulSoup(html, "html.parser")
    years: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        match = FUSEBOX_YEAR_RE.search(urlparse(href).path)
        if not match:
            continue
        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        years.append(
            {
                "year": match.group(1),
                "variant": match.group(2),
                "url": url,
                "label": clean_text(anchor.get_text(" ", strip=True)),
            }
        )
    year_sample: dict[str, Any] | None = None
    if years:
        y_status, y_html, y_final = fetch(session, str(years[0]["url"]))
        boxes = parse_window_json(y_html, "fuseboxesData")
        if not isinstance(boxes, list):
            boxes = []
        slim_boxes = []
        for box in boxes[:4]:
            if not isinstance(box, dict):
                continue
            diagram = box.get("boxDiagramImg") if isinstance(box.get("boxDiagramImg"), dict) else {}
            tables = box.get("boxTablesImages") if isinstance(box.get("boxTablesImages"), list) else []
            fuses = box.get("fuses") if isinstance(box.get("fuses"), list) else []
            described = [f for f in fuses if isinstance(f, dict) and f.get("description")]
            slim_boxes.append(
                {
                    "box_name": box.get("boxName"),
                    "n_fuses": len(fuses),
                    "n_described_fuses": len(described),
                    "diagram_url": diagram.get("url") if isinstance(diagram, dict) else None,
                    "thumbnail": box.get("boxThumbnail"),
                    "table_image_count": len(tables),
                }
            )
        y_soup = BeautifulSoup(y_html, "html.parser")
        year_sample = {
            "url": y_final,
            "status": y_status,
            "html_bytes": len(y_html),
            "title": clean_text(y_soup.title.get_text(strip=True) if y_soup.title else None),
            "box_count": len(boxes),
            "boxes": slim_boxes,
        }
    return {
        "listing_url": final,
        "status": status,
        "year_page_count": len(years),
        "years_sample": years[:8],
        "year_sample_detail": year_sample,
    }


def probe_manuals(session: requests.Session, model_path: str) -> dict[str, Any]:
    listing_url = urljoin(BASE_URL, f"/{model_path}/info/manuals")
    status, html, final = fetch(session, listing_url)
    soup = BeautifulSoup(html, "html.parser")
    year_links = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        path = urlparse(href).path
        if "/info/manuals/" not in path or path.rstrip("/").endswith("/upload"):
            continue
        url = urljoin(BASE_URL, href)
        if url in seen or "service-repair" in path:
            continue
        seen.add(url)
        year_links.append({"url": url, "label": clean_text(anchor.get_text(" ", strip=True))})
    pdfs: list[dict[str, str | None]] = []
    year_sample = year_links[0]["url"] if year_links else None
    if year_sample:
        _st, y_html, y_final = fetch(session, year_sample)
        ysoup = BeautifulSoup(y_html, "html.parser")
        for anchor in ysoup.find_all("a", href=True):
            href = anchor["href"]
            if not PDF_HREF_RE.search(href):
                continue
            pdfs.append(
                {
                    "url": urljoin(BASE_URL, href),
                    "label": clean_text(anchor.get_text(" ", strip=True)),
                }
            )
        year_sample = y_final
    return {
        "listing_url": final,
        "status": status,
        "year_page_count": len(year_links),
        "years_sample": year_links[:8],
        "pdf_sample_url": year_sample,
        "pdfs": pdfs[:8],
    }


def probe_hub(session: requests.Session, model_url: str) -> dict[str, Any]:
    status, html, final = fetch(session, model_url)
    soup = BeautifulSoup(html, "html.parser")
    page_modelo = extract_page_modelo(html)
    car_images = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not src or any(x in src for x in ("logo", "icon", "bandera", "design2", "favicon")):
            continue
        car_images.append(urljoin(BASE_URL, src))
    return {
        "url": final,
        "status": status,
        "html_bytes": len(html),
        "title": clean_text(soup.title.get_text(strip=True) if soup.title else None),
        "anti_bot": detect_anti_bot(html),
        "has_json_ld": bool(soup.find("script", type="application/ld+json")),
        "has_next_data": "__NEXT_DATA__" in html,
        "has_nuxt": "__NUXT__" in html,
        "page_modelo": page_modelo,
        "hub_links": extract_hub_links(html),
        "non_ui_images": car_images[:12],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe startmycar.com /pickmake catalog.")
    parser.add_argument("--limit-brands", type=int, default=3)
    parser.add_argument("--sample-model", default="toyota/camry", help="brand/model path to deep-probe")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    try:
        _print_section("0) robots.txt + anti-bot")
        robots_status, robots_body, _ = fetch(session, f"{BASE_URL}/robots.txt")
        print(f"[+] robots.txt status={robots_status} bytes={len(robots_body)}")
        disallow_catalog = any(
            line.lower().startswith("disallow:") and any(p in line.lower() for p in ("/pickmake", "/info/", "/trims"))
            for line in robots_body.splitlines()
        )
        print(f"[=] catalog paths disallowed={disallow_catalog}")

        _print_section("1) Brand listing /pickmake")
        status, html, final = fetch(session, LISTING_URL)
        print(f"[+] GET {final} status={status} bytes={len(html):,}")
        markers = detect_anti_bot(html)
        print(f"[=] anti-bot markers={markers or 'none'}")
        RAW_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_LISTING_PATH.write_text(html, encoding="utf-8")
        brands = extract_brands(html)
        iranish = [b for b in brands if any(h in b["slug"] for h in IRAN_BRAND_HINTS)]
        print(f"[=] brands={len(brands)} iran-oem-like={len(iranish)}")
        for brand in brands[:12]:
            print(f"    - {brand['name']} -> {brand['url']}")
        if args.verbose:
            print("    slugs:", ", ".join(b["slug"] for b in brands))

        _print_section("2) Brand pages → models")
        brand_samples: list[dict[str, Any]] = []
        for brand in brands[: max(1, args.limit_brands)]:
            b_status, b_html, b_final = fetch(session, brand["url"])
            models = extract_models(b_html, brand["slug"])
            print(f"[+] {brand['name']}: status={b_status} models={len(models)}")
            brand_samples.append(
                {
                    **brand,
                    "status": b_status,
                    "final_url": b_final,
                    "model_count": len(models),
                    "models_sample": models[:8],
                }
            )

        _print_section("3) Model hub + trims/specs/photos")
        model_path = args.sample_model.strip("/")
        hub = probe_hub(session, urljoin(BASE_URL, f"/{model_path}"))
        print(
            f"[+] hub {hub['url']} status={hub['status']} "
            f"jsonld={hub['has_json_ld']} next={hub['has_next_data']} images={len(hub['non_ui_images'])}"
        )
        print(f"    hub_links={list(hub['hub_links'])}")
        if hub.get("page_modelo"):
            pm = hub["page_modelo"]
            print(f"    pageModelo id={pm.get('idModelo')} countries={pm.get('countries')} denuncias={pm.get('denunciasCount')}")

        trims = probe_trims(session, model_path)
        print(f"[+] trims {trims['url']} empty={trims.get('empty_message')!r} robots={trims.get('robots')}")

        engine = probe_engine_guide(session, model_path)
        print(
            f"[+] engine guide generic={engine['looks_generic_explainer']} "
            f"table={engine['has_spec_table']} chars={engine['article_chars']}"
        )

        _print_section("4) Fuse box + manuals (actual extractable content)")
        fusebox = probe_fusebox(session, model_path)
        print(f"[+] fusebox years={fusebox['year_page_count']}")
        detail = fusebox.get("year_sample_detail") or {}
        if detail:
            print(f"    sample {detail.get('url')} boxes={detail.get('box_count')}")
            for box in detail.get("boxes") or []:
                print(
                    f"      - {box.get('box_name')}: fuses={box.get('n_fuses')} "
                    f"described={box.get('n_described_fuses')} diagram={bool(box.get('diagram_url'))}"
                )

        manuals = probe_manuals(session, model_path)
        print(f"[+] manuals years={manuals['year_page_count']} pdfs_on_sample={len(manuals.get('pdfs') or [])}")
        for pdf in (manuals.get("pdfs") or [])[:3]:
            print(f"    pdf {pdf.get('url')}")

    except Timeout as exc:
        print(f"[!] timeout: {exc}", file=sys.stderr)
        return 2
    except RequestException as exc:
        print(f"[!] request failed: {exc}", file=sys.stderr)
        return 2

    recommendation = {
        "can_crawl": True,
        "preferred_strategy": "html_pickmake_then_model_hub",
        "good_for_car_specs": False,
        "good_for_car_photos": False,
        "good_for": [
            "make/model catalog",
            "fuse box diagrams + structured fuse tables (window.fuseboxesData)",
            "owner manual PDFs",
            "problems/reviews (community, English)",
        ],
        "reason": (
            "SSR HTML, no Cloudflare, robots allow catalog. Trim/spec pages are empty "
            "placeholders. Car galleries do not exist. Fuse-box JSON + manual PDFs "
            "are the dense extractable assets. Not a replacement for khodrobank/bama specs."
        ),
        "anti_bot": "none on HTML GET; botFilterFieldJS only on write forms",
        "brand_count": len(brands),
        "iran_oem_brands": [b["slug"] for b in iranish],
        "output_suggestion": "data/raw_startmycar_models.jsonl (only if fuse/manuals are in scope)",
        "record_fields_if_crawled": [
            "brand",
            "brand_slug",
            "model",
            "model_slug",
            "url",
            "logo",
            "fusebox_years",
            "manual_pdfs",
            "problems_count",
            "crawled_at",
            "source",
        ],
    }

    report = {
        "probed_at": _now_iso(),
        "source": "startmycar",
        "listing": {
            "url": final,
            "status": status,
            "brand_count": len(brands),
            "brands_sample": brands[:25],
            "iran_oem_like": iranish,
            "raw_html_path": str(RAW_LISTING_PATH),
            "anti_bot": markers,
            "robots_catalog_disallowed": disallow_catalog,
        },
        "brand_pages": brand_samples,
        "hub": hub,
        "trims": trims,
        "engine_guide": engine,
        "fusebox": fusebox,
        "manuals": manuals,
        "recommendation": recommendation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[+] wrote {args.output.resolve()}")

    _print_section("5) Recommendation")
    print(json.dumps(recommendation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
