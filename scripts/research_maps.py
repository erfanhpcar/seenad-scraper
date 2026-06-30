#!/usr/bin/env python3
"""
Research probe: compare direct API vs static HTML strategies for Balad and Neshan.

Targets:
  - Balad branch listing: https://balad.ir/branch/tehran-pars-khodro
  - Neshan web map search: https://neshan.org/maps/search/پارس%20خودرو

Scenarios:
  A) Direct API requests (requests + standard browser headers)
  B) Static HTML fetching (__NEXT_DATA__, JSON-LD, embedded script JSON)

Usage:
  python scripts/research_maps.py
  python scripts/research_maps.py --balad-url https://balad.ir/branch/tehran-pars-khodro
  python scripts/research_maps.py --neshan-term "پارس خودرو"

Dependencies:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException

ContentKind = Literal["json", "html", "security_block", "unknown", "error"]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_BALAD_URL = "https://balad.ir/branch/tehran-pars-khodro"
DEFAULT_NESHAN_TERM = "پارس خودرو"
REQUEST_TIMEOUT_S = 25

ANTI_BOT_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "turnstile",
    "arvancloud",
    "access denied",
    "captcha",
    "security check",
    "please enable javascript",
)


@dataclass
class HttpProbeResult:
    label: str
    url: str
    method: str = "GET"
    status_code: int | None = None
    byte_size: int = 0
    content_type: str = ""
    content_kind: ContentKind = "unknown"
    success: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    json_keys: list[str] = field(default_factory=list)
    body_preview: str = ""


@dataclass
class HtmlProbeResult:
    url: str
    status_code: int | None = None
    byte_size: int = 0
    content_type: str = ""
    content_kind: ContentKind = "unknown"
    error: str | None = None
    framework: dict[str, Any] = field(default_factory=dict)
    anti_bot: dict[str, Any] = field(default_factory=dict)
    payload_signals: dict[str, Any] = field(default_factory=dict)
    dealership_records: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PlatformReport:
    name: str
    target_url: str
    api_probes: list[HttpProbeResult] = field(default_factory=list)
    html_probe: HtmlProbeResult | None = None
    discovered_api_hints: list[str] = field(default_factory=list)


def build_session(referer: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
        }
    )
    return session


def classify_body(
    status_code: int | None,
    content_type: str,
    body: str,
) -> ContentKind:
    lowered = body.lower()
    if status_code in {403, 401, 429, 503} or any(marker in lowered for marker in ANTI_BOT_MARKERS):
        return "security_block"

    if "application/json" in content_type.lower():
        return "json"

    stripped = body.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(body)
            return "json"
        except json.JSONDecodeError:
            pass

    if "<html" in lowered or "<!doctype html" in lowered:
        return "html"

    if status_code == 200 and body.strip() in {"200", "OK"}:
        return "security_block"

    return "unknown"


def safe_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    label: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> HttpProbeResult:
    result = HttpProbeResult(label=label, url=url, method=method.upper())
    merged_headers = dict(session.headers)
    if headers:
        merged_headers.update(headers)

    try:
        response = session.request(
            method=method.upper(),
            url=url,
            headers=merged_headers,
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT_S,
        )
        response.encoding = response.encoding or "utf-8"
        body = response.text
        result.status_code = response.status_code
        result.byte_size = len(response.content)
        result.content_type = response.headers.get("content-type", "")
        result.content_kind = classify_body(response.status_code, result.content_type, body)
        result.body_preview = body[:240].replace("\n", " ")
        result.success = (
            response.status_code == 200
            and result.content_kind == "json"
            and not _json_is_error_stub(body)
        )

        if result.content_kind == "json":
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    result.json_keys = list(payload.keys())[:20]
                    result.notes.extend(_summarize_json_payload(payload))
            except ValueError:
                result.notes.append("Content-Type claims JSON but body is not valid JSON.")

    except RequestException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.content_kind = "error"

    return result


def _json_is_error_stub(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip() in {"200", "OK"}
    if not isinstance(payload, dict):
        return False
    if payload.get("status") == "ERROR":
        return True
    if payload.get("detail") == "Not found.":
        return True
    return False


def _summarize_json_payload(payload: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if payload.get("status") == "ERROR":
        notes.append(f"API error code={payload.get('code')} message={payload.get('message')}")
    if payload.get("detail"):
        notes.append(f"detail={payload['detail']}")

    items = payload.get("items")
    if isinstance(items, list):
        if items and isinstance(items[0], dict):
            notes.append(f"items={len(items)} rich_objects keys={list(items[0].keys())[:8]}")
        elif items and isinstance(items[0], str):
            notes.append(f"items={len(items)} token_ids_only (needs POI hydration)")
        else:
            notes.append(f"items={len(items)}")

    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data_items = data["items"]
        if data_items and isinstance(data_items[0], dict):
            notes.append(
                f"data.items={len(data_items)} rich_objects keys={list(data_items[0].keys())[:8]}"
            )

    if isinstance(payload.get("page_count"), int):
        notes.append(f"page_count={payload['page_count']}")

    return notes


def detect_anti_bot(status: int | None, html: str, headers: dict[str, str]) -> dict[str, Any]:
    lowered = html.lower()
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    return {
        "http_status": status,
        "blocked_likely": (status in {403, 429, 503} if status else False)
        or any(marker in lowered for marker in ANTI_BOT_MARKERS),
        "cloudflare_signals": "cf-ray" in normalized_headers or "cloudflare" in lowered[:8000],
        "arvan_signals": "arvan" in str(normalized_headers).lower() or "arvancloud" in lowered[:8000],
        "cdn_server": normalized_headers.get("server"),
        "challenge_page_detected": any(marker in lowered for marker in ANTI_BOT_MARKERS),
    }


def detect_rendering_framework(html: str) -> dict[str, Any]:
    return {
        "framework": (
            "nextjs"
            if "__NEXT_DATA__" in html
            else "react_spa"
            if '<div id="root"></div>' in html or '<div id="root">' in html
            else "unknown"
        ),
        "has___NEXT_DATA__": "__NEXT_DATA__" in html,
        "has_self___next_f": "self.__next_f.push" in html,
        "has_json_ld": 'type="application/ld+json"' in html,
        "is_spa_shell": '<noscript>You need to enable JavaScript' in html,
    }


def extract_balad_branch_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", maxsplit=1)[-1]


def extract_next_data(html: str) -> dict[str, Any] | None:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_json_ld_records(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        blocks = payload if isinstance(payload, list) else [payload]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("@type")
            if block_type == "ItemList":
                for element in block.get("itemListElement", []):
                    if not isinstance(element, dict):
                        continue
                    records.append(
                        {
                            "source": "json_ld_item_list",
                            "position": element.get("position"),
                            "url": element.get("url"),
                        }
                    )
            elif block_type == "LocalBusiness":
                records.append(
                    {
                        "source": "json_ld_local_business",
                        "name": block.get("name"),
                        "url": block.get("url"),
                        "telephone": block.get("telephone"),
                        "address": block.get("address"),
                    }
                )

    return records


def extract_script_json_blobs(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    blobs: list[dict[str, Any]] = []

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or len(text) < 40:
            continue
        if script.get("id") == "__NEXT_DATA__":
            continue
        if script.get("type") == "application/ld+json":
            continue
        if not any(keyword in text for keyword in ('"items"', '"telephone"', '"geometry"', '"token"')):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            blobs.append(payload)

    return blobs


def normalize_balad_dealership_records(next_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not next_data:
        return []

    page_props = next_data.get("props", {}).get("pageProps", {})
    data = page_props.get("data")
    if not isinstance(data, dict):
        return []

    items = data.get("items")
    if not isinstance(items, list):
        return []

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        geometry = item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
        coordinates = geometry.get("coordinates") if isinstance(geometry.get("coordinates"), list) else []
        records.append(
            {
                "source": "__NEXT_DATA__",
                "token": item.get("token"),
                "name": item.get("name"),
                "address": item.get("address"),
                "telephone": item.get("telephone"),
                "url_title": item.get("urlTitle"),
                "latitude": coordinates[1] if len(coordinates) > 1 else None,
                "longitude": coordinates[0] if len(coordinates) > 0 else None,
            }
        )
    return records


def scan_payload_signals(html: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    phones = [row.get("telephone") for row in records if row.get("telephone")]
    coords = [
        {"latitude": row.get("latitude"), "longitude": row.get("longitude")}
        for row in records
        if row.get("latitude") is not None and row.get("longitude") is not None
    ]
    tel_links = re.findall(r'href="tel:([^"]+)"', html)
    return {
        "dealership_records_found": len(records),
        "records_with_phone": len(phones),
        "records_with_coordinates": len(coords),
        "tel_links_in_dom": len(tel_links),
        "phone_sample": phones[:3],
        "coordinate_sample": coords[:2],
        "rich_ssr_payload": bool(records) and bool(phones or coords),
    }


def discover_api_hints_from_html(html: str) -> list[str]:
    service_domains = (
        "raah.ir",
        "api.balad.ir",
        "account.api.balad.ir",
        "neshanmap.ir",
        "api.neshan.org",
    )
    hints: set[str] = set()
    for match in re.findall(r"https://[a-z0-9.-]+[^\s\"'<>]*", html):
        if not any(domain in match for domain in service_domains):
            continue
        if any(noise in match for noise in ("/crowd-images/", "/_next/static/", ".jpg", ".png", ".woff")):
            continue
        hints.add(match.rstrip(".,;"))
    for href in re.findall(r'<link[^>]+href="(https://[^"]+)"', html):
        if any(domain in href for domain in service_domains):
            hints.add(href)
    return sorted(hints)


def discover_api_hints_from_js(session: requests.Session, script_urls: list[str], limit: int = 4) -> list[str]:
    hints: set[str] = set()
    for script_url in script_urls[:limit]:
        try:
            response = session.get(script_url, timeout=REQUEST_TIMEOUT_S)
            response.raise_for_status()
        except RequestException:
            continue
        js = response.text
        hints.update(re.findall(r"https://[a-z0-9.-]+\.(?:raah\.ir|balad\.ir|neshanmap\.ir)[^\s\"'<>]*", js))
        hints.update(re.findall(r"/v\d+/[a-zA-Z0-9/_-]+", js))
    return sorted(hints)


def collect_script_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for script in soup.find_all("script", src=True):
        src = script["src"]
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        elif not src.startswith("http"):
            src = base_url.rstrip("/") + "/" + src.lstrip("/")
        urls.append(src)
    return urls


def probe_balad_api_scenario(session: requests.Session, branch_url: str, slug: str) -> list[HttpProbeResult]:
    probes: list[HttpProbeResult] = []

    probes.append(
        safe_request(
            session,
            "GET",
            "https://search.raah.ir/v4/placeslist/branch/",
            label="search.raah.ir branch list (platform:web)",
            headers={
                "Accept": "application/json",
                "Origin": "https://balad.ir",
                "platform": "web",
            },
            params={"name": slug, "page": 1},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            "https://search.raah.ir/v4/placeslist/branch/",
            label="search.raah.ir branch list page 2",
            headers={
                "Accept": "application/json",
                "Origin": "https://balad.ir",
                "platform": "web",
            },
            params={"name": slug, "page": 2},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            f"https://api.balad.ir/v4/branch/{slug}",
            label="api.balad.ir branch (no platform header)",
            headers={"Accept": "application/json"},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            f"https://search.raah.ir/v4/placeslist/branch/{slug}",
            label="search.raah.ir branch path-style (likely 404)",
            headers={"Accept": "application/json", "platform": "web"},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            "https://poi.raah.ir/v4/poi/2xJ2651rbxjAaX",
            label="poi.raah.ir single POI token (sample)",
            headers={"Accept": "application/json", "platform": "web"},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            branch_url,
            label="branch page as JSON accept (should be HTML)",
            headers={"Accept": "application/json"},
        )
    )
    return probes


def probe_neshan_api_scenario(session: requests.Session, search_term: str) -> list[HttpProbeResult]:
    encoded_term = quote(search_term)
    probes: list[HttpProbeResult] = []

    for version in ("v1", "v2"):
        probes.append(
            safe_request(
                session,
                "GET",
                f"https://api.neshan.org/{version}/search",
                label=f"api.neshan.org {version} search (official, needs Api-Key)",
                headers={"Accept": "application/json"},
                params={"term": search_term},
            )
        )

    probes.append(
        safe_request(
            session,
            "GET",
            f"https://neshan.org/maps/api/search?term={encoded_term}",
            label="neshan.org/maps/api/search proxy",
            headers={"Accept": "application/json"},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            "https://vts1.neshanmap.ir/poi/v3/10/512/384/navigator",
            label="vts1.neshanmap.ir vector POI tile (not text search)",
            headers={"Accept": "application/json"},
        )
    )
    probes.append(
        safe_request(
            session,
            "GET",
            f"https://neshan.org/maps/search/{encoded_term}",
            label="neshan maps search route with JSON accept",
            headers={"Accept": "application/json"},
        )
    )
    return probes


def probe_html_scenario(
    session: requests.Session,
    url: str,
    *,
    platform: Literal["balad", "neshan"],
) -> HtmlProbeResult:
    result = HtmlProbeResult(url=url)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_S)
        response.encoding = "utf-8"
        html = response.text
        headers = {k.lower(): v for k, v in response.headers.items()}
        result.status_code = response.status_code
        result.byte_size = len(response.content)
        result.content_type = response.headers.get("content-type", "")
        result.content_kind = classify_body(response.status_code, result.content_type, html)
        result.anti_bot = detect_anti_bot(response.status_code, html, headers)
        result.framework = detect_rendering_framework(html)

        if platform == "balad":
            next_data = extract_next_data(html)
            records = normalize_balad_dealership_records(next_data)
            if not records:
                records = extract_json_ld_records(html)
            result.dealership_records = records[:25]
            result.payload_signals = scan_payload_signals(html, records)
            if next_data:
                page_props = next_data.get("props", {}).get("pageProps", {})
                data = page_props.get("data", {})
                if isinstance(data, dict):
                    result.notes.append(f"__NEXT_DATA__ page_count={data.get('pageCount')}")
                    result.notes.append(f"__NEXT_DATA__ items_on_page={len(data.get('items', []))}")
        else:
            json_ld_records = extract_json_ld_records(html)
            script_blobs = extract_script_json_blobs(html)
            result.dealership_records = json_ld_records[:25]
            result.payload_signals = {
                "dealership_records_found": len(json_ld_records),
                "embedded_script_json_blobs": len(script_blobs),
                "spa_shell_only": result.framework.get("is_spa_shell", False),
                "rich_ssr_payload": bool(json_ld_records) or bool(script_blobs),
            }
            if result.framework.get("is_spa_shell"):
                result.notes.append("Pure client-side React shell; search results are not SSR-embedded.")

        if result.anti_bot.get("blocked_likely"):
            result.content_kind = "security_block"
            result.notes.append("Anti-bot / challenge markers detected in HTML.")

    except RequestException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.content_kind = "error"

    return result


def run_balad_research(session: requests.Session, branch_url: str) -> PlatformReport:
    slug = extract_balad_branch_slug(branch_url)
    report = PlatformReport(name="Balad", target_url=branch_url)

    html_probe = probe_html_scenario(session, branch_url, platform="balad")
    report.html_probe = html_probe

    if html_probe.status_code == 200 and not html_probe.error:
        try:
            response = session.get(branch_url, timeout=REQUEST_TIMEOUT_S)
            report.discovered_api_hints = discover_api_hints_from_html(response.text)
            script_urls = collect_script_urls(response.text, branch_url)
            cdn_scripts = [url for url in script_urls if "balad.ir" in url or "raah.ir" in url]
            report.discovered_api_hints.extend(
                hint for hint in discover_api_hints_from_js(session, cdn_scripts) if hint.startswith("http")
            )
        except RequestException:
            pass

    report.api_probes = probe_balad_api_scenario(session, branch_url, slug)
    return report


def run_neshan_research(session: requests.Session, search_term: str) -> PlatformReport:
    encoded_term = quote(search_term)
    search_url = f"https://neshan.org/maps/search/{encoded_term}"
    report = PlatformReport(name="Neshan", target_url=search_url)

    html_probe = probe_html_scenario(session, search_url, platform="neshan")
    report.html_probe = html_probe

    if html_probe.status_code == 200 and not html_probe.error:
        try:
            maps_home = session.get("https://neshan.org/maps", timeout=REQUEST_TIMEOUT_S)
            report.discovered_api_hints = discover_api_hints_from_html(maps_home.text)
            script_urls = collect_script_urls(maps_home.text, "https://neshan.org/maps/")
            report.discovered_api_hints.extend(
                hint
                for hint in discover_api_hints_from_js(session, script_urls)
                if hint.startswith("http")
            )
        except RequestException:
            pass

    report.api_probes = probe_neshan_api_scenario(session, search_term)
    return report


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    return f"{num_bytes / 1024:.1f} KB"


def print_http_probe_row(probe: HttpProbeResult) -> None:
    status = probe.status_code if probe.status_code is not None else "ERR"
    print(
        f"  - [{probe.label}] "
        f"status={status} size={_format_bytes(probe.byte_size)} "
        f"kind={probe.content_kind} content_type={probe.content_type or '-'}"
    )
    print(f"    URL: {probe.url}")
    if probe.error:
        print(f"    ERROR: {probe.error}")
    if probe.json_keys:
        print(f"    JSON keys: {', '.join(probe.json_keys[:12])}")
    for note in probe.notes:
        print(f"    note: {note}")
    if probe.body_preview and probe.content_kind in {"security_block", "unknown", "error"}:
        print(f"    preview: {probe.body_preview[:180]}")


def print_html_probe(report: HtmlProbeResult) -> None:
    status = report.status_code if report.status_code is not None else "ERR"
    print(
        f"  status={status} size={_format_bytes(report.byte_size)} "
        f"kind={report.content_kind} content_type={report.content_type or '-'}"
    )
    print(f"  URL: {report.url}")
    if report.error:
        print(f"  ERROR: {report.error}")
    print(f"  framework: {report.framework.get('framework')} | anti_bot.blocked_likely={report.anti_bot.get('blocked_likely')}")
    signals = report.payload_signals
    print(
        "  payload: "
        f"records={signals.get('dealership_records_found', 0)} "
        f"phones={signals.get('records_with_phone', signals.get('tel_links_in_dom', 0))} "
        f"coords={signals.get('records_with_coordinates', 0)} "
        f"rich_ssr={signals.get('rich_ssr_payload', False)}"
    )
    for note in report.notes:
        print(f"  note: {note}")
    if report.dealership_records:
        sample = report.dealership_records[0]
        print(f"  sample_record: {json.dumps(sample, ensure_ascii=False)[:220]}")


def print_platform_report(report: PlatformReport) -> None:
    print("\n" + "=" * 78)
    print(f"PLATFORM: {report.name}")
    print(f"Target: {report.target_url}")
    print("=" * 78)

    print("\n[Scenario A] Direct API Requests")
    if report.discovered_api_hints:
        print("  Discovered hints:")
        for hint in report.discovered_api_hints[:12]:
            print(f"    - {hint}")
    for probe in report.api_probes:
        print_http_probe_row(probe)

    print("\n[Scenario B] Static HTML Fetching")
    if report.html_probe:
        print_html_probe(report.html_probe)
    else:
        print("  No HTML probe executed.")


def _best_api_probe(probes: list[HttpProbeResult]) -> HttpProbeResult | None:
    successful = [probe for probe in probes if probe.success]
    if not successful:
        return None
    return max(successful, key=lambda probe: probe.byte_size)


def recommend_balad(report: PlatformReport) -> str:
    html = report.html_probe
    api_best = _best_api_probe(report.api_probes)

    if html and html.anti_bot.get("blocked_likely"):
        return (
            "Playwright یا پروکسی مرورگر — درخواست‌های مستقیم توسط لایه امنیتی/CDN مسدود می‌شوند."
        )

    html_rich = bool(html and html.payload_signals.get("rich_ssr_payload"))
    api_usable = api_best is not None

    if html_rich and api_usable:
        return (
            "ترکیب بهینه: (۱) پارس HTML استاتیک — استخراج `__NEXT_DATA__` برای صفحه اول با داده غنی "
            "(نام، آدرس، تلفن، مختصات)؛ (۲) API مستقیم `search.raah.ir/v4/placeslist/branch/` "
            "با هدر `platform: web` برای صفحه‌بندی. توجه: پاسخ API فقط توکن POI می‌دهد؛ "
            "برای جزئیات کامل هر شعبه یا همان SSR صفحه branch کافی است یا fetch صفحات `/p/`."
        )

    if html_rich:
        return (
            "پارس HTML استاتیک (`__NEXT_DATA__` + JSON-LD) — پایدارترین روش برای بلد؛ "
            "داده نمایندگی‌ها در SSR هاردکد شده و بدون اجرای JavaScript قابل استخراج است."
        )

    if api_usable:
        return (
            "API مستقیم `search.raah.ir` — JSON تمیز با هدر `platform: web`؛ "
            "برای داده کامل POI احتمالاً نیاز به hydrate توکن‌ها یا crawl صفحات پروفایل."
        )

    return (
        "Playwright — نه API عمومی بدون احراز هویت، نه SSR غنی؛ "
        "احتمالاً داده پس از hydration کلاینت لود می‌شود."
    )


def recommend_neshan(report: PlatformReport) -> str:
    html = report.html_probe
    api_probes = report.api_probes

    official_api = next(
        (
            probe
            for probe in api_probes
            if "api.neshan.org" in probe.url
            and probe.content_kind == "json"
            and probe.status_code == 480
        ),
        None,
    )
    api_key_required = official_api is not None

    if html and html.anti_bot.get("blocked_likely"):
        return (
            "Playwright — صفحه HTML توسط فایروال/challenge مسدود شده است."
        )

    spa_shell = bool(html and html.framework.get("is_spa_shell"))
    html_rich = bool(html and html.payload_signals.get("rich_ssr_payload"))

    if api_key_required and not html_rich:
        return (
            "API رسمی نشان (`api.neshan.org/v1|v2/search`) با کلید `Api-Key` — "
            "درخواست مستقیم بدون کلید خطای ۴۸۰ می‌دهد (endpoint معتبر اما نیازمند احراز هویت). "
            "HTML استاتیک فقط shell اپ React است و نتایج جستجو SSR نشده‌اند. "
            "برای مهندسی معکوس بدون کلید API: Playwright + intercept شبکه برای capture "
            "درخواست‌های داخلی مرورگر؛ یا ثبت‌نام در platform.neshan.org و استفاده از API رسمی."
        )

    if html_rich:
        return (
            "پارس HTML استاتیک — داده‌های جستجو در HTML/script تعبیه شده‌اند."
        )

    if spa_shell:
        return (
            "Playwright (یا API رسمی با Api-Key) — وب‌اپ نشان یک SPA خالص است؛ "
            "`requests` + BeautifulSoup فقط shell خالی برمی‌گرداند. "
            "vector tileهای `neshanmap.ir` برای نقشه هستند، نه جستجوی متنی نمایندگی."
        )

    return (
        "Playwright — هیچ‌کدام از روش‌های سبک داده کافی برنگرداند."
    )


def print_recommendations(balad_report: PlatformReport, neshan_report: PlatformReport) -> None:
    print("\n" + "=" * 78)
    print("گزارش توصیه‌ای (Recommendation)")
    print("=" * 78)
    print("\n[بلد / Balad]")
    print(recommend_balad(balad_report))
    print("\n[نشان / Neshan]")
    print(recommend_neshan(neshan_report))
    print("\nخلاصه مقایسه:")
    print(
        "  بلد  → SSR غنی (`__NEXT_DATA__`) + API داخلی قابل استفاده با هدر platform:web"
    )
    print(
        "  نشان → SSR خالی (SPA) + API رسمی نیازمند Api-Key → Playwright یا API رسمی"
    )
    print("=" * 78)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research Balad vs Neshan crawling strategies (API vs static HTML vs Playwright)."
    )
    parser.add_argument("--balad-url", default=DEFAULT_BALAD_URL, help="Balad branch listing URL")
    parser.add_argument("--neshan-term", default=DEFAULT_NESHAN_TERM, help="Neshan search term")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    neshan_search_url = f"https://neshan.org/maps/search/{quote(args.neshan_term)}"

    print("Research Maps Probe")
    print(f"Balad target : {args.balad_url}")
    print(f"Neshan target: {neshan_search_url} (term={args.neshan_term!r})")
    print(f"User-Agent   : {USER_AGENT[:60]}...")

    balad_session = build_session(referer="https://balad.ir/")
    neshan_session = build_session(referer="https://neshan.org/maps/")

    balad_report = run_balad_research(balad_session, args.balad_url)
    neshan_report = run_neshan_research(neshan_session, args.neshan_term)

    print_platform_report(balad_report)
    print_platform_report(neshan_report)
    print_recommendations(balad_report, neshan_report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
