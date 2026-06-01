#!/usr/bin/env python3
"""
Diagnostic probe: Bahman Motor (بهمن موتور) branch/dealer page.

Target URL: https://bahman.ir/branch

This script does not scrape data. It systematically tries realistic browser
requests (headers, referer, SSL verify on/off) and logs why each attempt
fails or succeeds — useful when the site times out, drops connections, or
returns WAF blocks (ArvanCloud / Cloudflare / geo-restriction).

Usage:
  python scripts/probe_bahman_motor.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import urllib3
from requests.exceptions import (
    ConnectionError,
    ReadTimeout,
    RequestException,
    SSLError,
    Timeout,
)

TARGET_URL = "https://bahman.ir/branch"
REQUEST_TIMEOUT_S = 20
RAW_CAPTURE_PATH = Path("data/bahman_probe_raw.txt")
RAW_CAPTURE_MAX_CHARS = 25_000

# Signatures often present when a CDN/WAF sits in front of the origin.
CDN_WAF_HEADER_HINTS = (
    "cf-",
    "cloudflare",
    "arvan",
    "arvancode",
    "server",
    "x-cache",
    "x-served-by",
    "x-cdn",
    "x-protected-by",
    "x-firewall",
    "x-waf",
)

BASE_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


@dataclass(frozen=True)
class ProbeAttempt:
    """One labeled GET configuration to try against the target URL."""

    label: str
    headers: dict[str, str]
    verify_ssl: bool


def _build_probe_matrix() -> list[ProbeAttempt]:
    """Ordered attempts: strict SSL first, then insecure + referer variants."""
    home_referer = {**BASE_BROWSER_HEADERS, "Referer": "https://bahman.ir/"}
    branch_referer = {**BASE_BROWSER_HEADERS, "Referer": "https://bahman.ir/branch"}
    root_then_branch = {
        **BASE_BROWSER_HEADERS,
        "Referer": "https://www.bahman.ir/",
    }

    return [
        ProbeAttempt("01_strict_ssl_direct", BASE_BROWSER_HEADERS, verify_ssl=True),
        ProbeAttempt(
            "02_insecure_ssl_direct",
            BASE_BROWSER_HEADERS,
            verify_ssl=False,
        ),
        ProbeAttempt(
            "03_insecure_ssl_referer_home",
            home_referer,
            verify_ssl=False,
        ),
        ProbeAttempt(
            "04_insecure_ssl_referer_branch",
            branch_referer,
            verify_ssl=False,
        ),
        ProbeAttempt(
            "05_insecure_ssl_referer_www",
            root_then_branch,
            verify_ssl=False,
        ),
        ProbeAttempt(
            "06_insecure_ssl_minimal_accept",
            {
                "User-Agent": BASE_BROWSER_HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fa-IR,fa;q=0.9",
                "Referer": "https://bahman.ir/",
            },
            verify_ssl=False,
        ),
    ]


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _print_request_headers(headers: dict[str, str]) -> None:
    print("[=] Request headers:")
    for key, value in headers.items():
        print(f"    {key}: {value}")


def _highlight_cdn_waf_headers(response_headers: requests.structures.CaseInsensitiveDict[str]) -> None:
    """Flag response headers that may indicate CDN/WAF presence."""
    hints: list[str] = []
    for name, value in response_headers.items():
        combined = f"{name}: {value}".lower()
        if any(marker in combined for marker in CDN_WAF_HEADER_HINTS):
            hints.append(f"{name}: {value}")
    if hints:
        print("[!] Possible CDN/WAF-related response headers:")
        for line in hints:
            print(f"    {line}")
    else:
        print("[i] No obvious CDN/WAF header markers in response.")


def _print_response_diagnostics(response: requests.Response) -> None:
    content_type = response.headers.get("Content-Type", "(not set)")
    print(f"[=] Status Code: {response.status_code}")
    print(f"[=] Content-Type: {content_type}")
    print(f"[=] Body length: {len(response.content):,} bytes")
    print("[=] Response headers:")
    for name, value in response.headers.items():
        print(f"    {name}: {value}")
    _highlight_cdn_waf_headers(response.headers)
    if response.status_code != 200:
        preview = response.text[:400].replace("\n", " ")
        print(f"[=] Body preview (first 400 chars): {preview!r}")


def _save_raw_capture(text: str) -> None:
    snippet = text[:RAW_CAPTURE_MAX_CHARS]
    RAW_CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_CAPTURE_PATH.write_text(snippet, encoding="utf-8")
    print(
        f"[+] Saved first {len(snippet):,} characters to "
        f"{RAW_CAPTURE_PATH.resolve()}"
    )


def _execute_get(
    session: requests.Session,
    attempt: ProbeAttempt,
) -> tuple[requests.Response | None, str | None]:
    """
    Run a single GET. Returns (response, None) on success, or (None, error_label).
    """
    print(f"[+] URL: {TARGET_URL}")
    print(f"[+] verify SSL: {attempt.verify_ssl}")
    _print_request_headers(attempt.headers)

    try:
        response = session.get(
            TARGET_URL,
            headers=attempt.headers,
            timeout=REQUEST_TIMEOUT_S,
            verify=attempt.verify_ssl,
            allow_redirects=True,
        )
        return response, None
    except SSLError as exc:
        return None, f"SSLError: {exc}"
    except (Timeout, ReadTimeout) as exc:
        return None, f"Timeout ({REQUEST_TIMEOUT_S}s): {exc}"
    except ConnectionError as exc:
        return None, f"ConnectionError: {exc}"
    except RequestException as exc:
        return None, f"RequestException: {exc}"


def _run_attempt(
    session: requests.Session,
    attempt: ProbeAttempt,
) -> requests.Response | None:
    _print_section(f"Attempt: {attempt.label}")

    response, error = _execute_get(session, attempt)

    if error is not None:
        print(f"[!] Request failed — {error}")
        if "SSLError" in error and not attempt.verify_ssl:
            print(
                "[i] SSL failed even with verify=False; likely not a local cert issue."
            )
        elif "SSLError" in error and attempt.verify_ssl:
            print(
                "[i] Hint: a later attempt retries with verify=False "
                "(common for some Iranian corporate TLS setups)."
            )
        return None

    assert response is not None
    _print_response_diagnostics(response)

    if response.status_code == 200:
        print("[+] SUCCESS: HTTP 200 OK")
        _save_raw_capture(response.text)
        return response

    print(f"[-] Non-200 status ({response.status_code}); trying next strategy.")
    return None


def probe() -> int:
    """Run all probe attempts until one returns 200 or all are exhausted."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print("Bahman Motor connectivity probe")
    print(f"Target: {TARGET_URL}")
    print(f"Timeout: {REQUEST_TIMEOUT_S}s per attempt")

    session = requests.Session()
    attempts = _build_probe_matrix()

    for attempt in attempts:
        result = _run_attempt(session, attempt)
        if result is not None and result.status_code == 200:
            print("\n[+] At least one strategy returned 200 OK.")
            print(f"[+] Winning strategy label: {attempt.label}")
            return 0

    _print_section("Summary")
    print("[-] No attempt returned HTTP 200.")
    print("[i] Review errors above for Timeout / ConnectionError (geo/WAF/network)")
    print("[i] Review non-200 responses for 403/503/406 and CDN header hints.")
    return 1


if __name__ == "__main__":
    sys.exit(probe())
