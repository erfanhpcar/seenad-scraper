#!/usr/bin/env python3
"""
Probe: Modiran Khodro (مدیران خودرو) dealership network — session + AJAX.

Step 1: GET https://www.mvmco.ir/dealersmvm (cookies + CSRF meta)
Step 2: POST https://www.mvmco.ir/find/dealers (provinceId sample: Isfahan = 4)

Usage:
  python scripts/probe_modiran_khodro.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError, ReadTimeout, SSLError, Timeout

BASE_URL = "https://www.mvmco.ir"
DEALERS_PAGE_URL = f"{BASE_URL}/dealersmvm"
FIND_DEALERS_URL = f"{BASE_URL}/find/dealers"
SAMPLE_PROVINCE_ID = "4"
PROVINCE_JSON_PATH = Path("data/modiran_province_4_sample.json")
REQUEST_TIMEOUT_S = 15

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

GET_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _extract_csrf_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta is None:
        return None
    content = meta.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _post_headers(csrf_token: str) -> dict[str, str]:
    return {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": DEALERS_PAGE_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _save_province_json(payload: object) -> None:
    PROVINCE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PROVINCE_JSON_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4, ensure_ascii=False)
    print(f"[+] Saved JSON to: {PROVINCE_JSON_PATH.resolve()}")


def probe() -> int:
    session = requests.Session()

    print(f"[Step 1] GET {DEALERS_PAGE_URL}")
    try:
        get_response = session.get(
            DEALERS_PAGE_URL,
            headers=GET_HEADERS,
            timeout=REQUEST_TIMEOUT_S,
        )
    except SSLError as exc:
        print(f"[!] SSL error on GET: {exc}")
        return 1
    except (Timeout, ReadTimeout) as exc:
        print(f"[!] GET timed out after {REQUEST_TIMEOUT_S}s: {exc}")
        return 1
    except ConnectionError as exc:
        print(f"[!] GET connection error: {exc}")
        return 1
    except requests.RequestException as exc:
        print(f"[!] GET request failed: {exc}")
        return 1

    print(f"[=] GET status: {get_response.status_code}")
    print(f"[=] GET content length: {len(get_response.content):,} bytes")
    print(f"[=] Session cookies: {list(session.cookies.keys())}")

    if get_response.status_code != 200:
        print("[-] GET failed; cannot obtain CSRF token.")
        return 1

    csrf_token = _extract_csrf_token(get_response.text)
    if not csrf_token:
        print("[-] CSRF token not found in <meta name=\"csrf-token\">.")
        return 1

    print(f"[=] Extracted CSRF token: {csrf_token[:48]}...")

    post_payload = {"provinceId": SAMPLE_PROVINCE_ID}
    print(f"\n[Step 2] POST {FIND_DEALERS_URL}")
    print(f"[=] POST data: {post_payload}")

    try:
        post_response = session.post(
            FIND_DEALERS_URL,
            headers=_post_headers(csrf_token),
            data=post_payload,
            timeout=REQUEST_TIMEOUT_S,
        )
    except SSLError as exc:
        print(f"[!] SSL error on POST: {exc}")
        return 1
    except (Timeout, ReadTimeout) as exc:
        print(f"[!] POST timed out after {REQUEST_TIMEOUT_S}s: {exc}")
        return 1
    except ConnectionError as exc:
        print(f"[!] POST connection error: {exc}")
        return 1
    except requests.RequestException as exc:
        print(f"[!] POST request failed: {exc}")
        return 1

    print(f"[=] POST status: {post_response.status_code}")
    print(f"[=] POST Content-Type: {post_response.headers.get('Content-Type', '(not set)')}")

    if post_response.status_code != 200:
        print(f"[-] POST body preview: {post_response.text[:500]}")
        return 1

    try:
        payload = post_response.json()
    except json.JSONDecodeError as exc:
        print(f"[!] POST response is not valid JSON: {exc}")
        print(f"[-] Body preview: {post_response.text[:500]}")
        return 1

    try:
        _save_province_json(payload)
    except OSError as exc:
        print(f"[!] Failed to write sample file: {exc}")
        return 1

    if isinstance(payload, dict):
        top_keys = list(payload.keys())
        print(f"[=] JSON top-level keys: {top_keys}")
        dealers = payload.get("dealers") or payload.get("dealerList")
        if isinstance(dealers, list):
            print(f"[=] Dealer records in response: {len(dealers)}")

    return 0


if __name__ == "__main__":
    sys.exit(probe())
