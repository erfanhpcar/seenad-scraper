#!/usr/bin/env python3
"""
Crawler: Ilia Auto (ایلیا موتور) sales & service center network.

Dealer data is server-rendered as vertical key-value tables (class
``ilia-dealer-table``) on the public branch locator page. Each table is preceded
by an ``ilia-dealer-table-header`` element with the dealership title.

Usage:
  python scripts/crawl_ilia.py
  python scripts/crawl_ilia.py --output data/raw_ilia_dealers.json -v

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("ilia-dealers-crawler")

TARGET_URL = (
    "https://ilia-auto.com/"
    "%d9%85%d8%b1%d8%a7%da%a9%d8%b2-%d9%81%d8%b1%d9%88%d8%b4-%d9%88-%d8%ae%d8%af%d9%85%d8%a7%d8%aa/"
)
FALLBACK_HTML_PATH = Path("data/ilia_probe_raw.html")
DEFAULT_OUTPUT = Path("data/raw_ilia_dealers.json")
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

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
PHONE_SPLIT_RE = re.compile(r"[,،/]+|(?:\s*-\s*)")
PHONE_DIGIT_RE = re.compile(r"\d+")
HEADER_CODE_RE = re.compile(r"کد\s*(\d+)", re.I)


@dataclass
class DealershipRecord:
    dealer_code: str | None
    name: str | None
    manager: str | None
    province: str | None
    city: str | None
    address: str | None
    phone_numbers: list[str] = field(default_factory=list)
    agency_type: str | None = None
    source: str = "ilia-auto.com"
    source_url: str = TARGET_URL
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    raw_row: dict[str, Any] = field(default_factory=dict, repr=False)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def clean_farsi_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def normalize_dealer_code(value: str | None) -> str | None:
    text = clean_farsi_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits or text


def clean_phone_numbers(*values: str | None) -> list[str]:
    """Split multi-number strings and preserve leading zeros."""
    phones: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = clean_farsi_text(value)
        if not text:
            continue

        for chunk in PHONE_SPLIT_RE.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue

            # Local-area pattern like 32377788-044 -> 04432377788
            local_area = re.match(r"^(\d{7,8})\s*-\s*(0\d{2,3})$", chunk)
            if local_area:
                chunk = f"{local_area.group(2)}{local_area.group(1)}"

            digits_only = "".join(PHONE_DIGIT_RE.findall(chunk))
            if not digits_only:
                continue

            if len(digits_only) < 10:
                continue

            if digits_only not in seen:
                seen.add(digits_only)
                phones.append(digits_only)

    return phones


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("<!DOCTYPE", "<html", "<HTML"))


def fetch_html() -> tuple[str, str]:
    """
    Try live fetch first; fall back to saved probe HTML on failure.

    Returns (html, source_label).
    """
    print(f"[+] Fetching live page: {TARGET_URL}")
    try:
        response = requests.get(
            TARGET_URL,
            headers=BROWSER_HEADERS,
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
    except SSLError as exc:
        print(f"[!] Live fetch SSL error: {exc}")
    except (Timeout, ReadTimeout) as exc:
        print(f"[!] Live fetch timed out after {REQUEST_TIMEOUT_S}s: {exc}")
    except ConnectionError as exc:
        print(f"[!] Live fetch connection error: {exc}")
    except RequestException as exc:
        print(f"[!] Live fetch failed: {exc}")
    else:
        content_encoding = response.headers.get("Content-Encoding", "")
        if response.status_code == 200 and _looks_like_html(response.text) and content_encoding != "br":
            print(
                f"[+] Live fetch OK ({len(response.content):,} bytes, "
                f"Content-Encoding={content_encoding or 'none'})"
            )
            return response.text, "live"

        print(
            f"[!] Live fetch unusable: status={response.status_code}, "
            f"Content-Encoding={content_encoding!r}, html_like={_looks_like_html(response.text)}"
        )

    if not FALLBACK_HTML_PATH.is_file():
        raise RuntimeError(
            f"Live fetch failed and fallback file not found: {FALLBACK_HTML_PATH.resolve()}"
        )

    print(f"[+] Falling back to local HTML: {FALLBACK_HTML_PATH.resolve()}")
    html = FALLBACK_HTML_PATH.read_text(encoding="utf-8")
    if not _looks_like_html(html):
        raise RuntimeError(f"Fallback file does not look like HTML: {FALLBACK_HTML_PATH}")
    return html, "fallback"


def _map_label_to_field(label: str) -> str | None:
    """Map a Persian row label to a standard schema key."""
    normalized = clean_farsi_text(label) or label

    if normalized in {"نام", "نمایندگی"} or (
        "نام" in normalized and "نمای" in normalized
    ):
        return "name"
    if normalized == "کد":
        return "dealer_code"
    if "مدیریت" in normalized or "صاحب امتیاز" in normalized:
        return "manager"
    if normalized == "استان":
        return "province"
    if normalized == "شهر":
        return "city"
    if "تلفن" in normalized or "تماس" in normalized or normalized == "فکس":
        return "phone_numbers"
    if normalized == "آدرس":
        return "address"
    if normalized in {"نوع فعالیت", "خدمات"} or (
        "نوع" in normalized and "فعال" in normalized
    ):
        return "agency_type"
    return None


def _extract_row_pairs(table: Tag) -> list[tuple[str, str]]:
    """Extract label/value pairs from a vertical dealer table."""
    pairs: list[tuple[str, str]] = []

    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue

        label = clean_farsi_text(cells[0].get_text(" ", strip=True))
        if not label:
            continue

        values = [
            clean_farsi_text(cell.get_text(" ", strip=True)) or ""
            for cell in cells[1:]
        ]
        value = " | ".join(value for value in values if value)
        pairs.append((label, value))

    return pairs


def _store_raw_value(raw_row: dict[str, Any], label: str, value: str) -> None:
    if not value:
        return
    existing = raw_row.get(label)
    if existing is None:
        raw_row[label] = value
    elif isinstance(existing, list):
        existing.append(value)
    else:
        raw_row[label] = [existing, value]


def _name_from_header(header_text: str | None) -> str | None:
    text = clean_farsi_text(header_text)
    if not text:
        return None
    # Example: "نمایندگی کد 103 - شیری - حنیفی"
    without_prefix = re.sub(r"^نمایندگی\s+", "", text, flags=re.I).strip()
    without_code = re.sub(r"^کد\s*\d+\s*[-–—]?\s*", "", without_prefix, flags=re.I).strip()
    return without_code or text


def _dealer_code_from_header(header_text: str | None) -> str | None:
    if not header_text:
        return None
    match = HEADER_CODE_RE.search(header_text)
    if match:
        return match.group(1)
    return None


def parse_dealer_tables(soup: BeautifulSoup) -> list[DealershipRecord]:
    records: list[DealershipRecord] = []
    tables = soup.find_all("table", class_="ilia-dealer-table")

    for table_index, table in enumerate(tables):
        pairs = _extract_row_pairs(table)
        if not pairs:
            LOGGER.debug("Skipping empty table #%d", table_index)
            continue

        header_el = table.find_previous(class_="ilia-dealer-table-header")
        header_text = header_el.get_text(" ", strip=True) if header_el else None

        raw_row: dict[str, Any] = {}
        if header_text:
            raw_row["__header"] = clean_farsi_text(header_text) or header_text

        mapped: dict[str, Any] = {
            "dealer_code": None,
            "name": None,
            "manager": None,
            "province": None,
            "city": None,
            "address": None,
            "agency_type": None,
        }
        phone_values: list[str] = []

        for label, value in pairs:
            _store_raw_value(raw_row, label, value)
            field_name = _map_label_to_field(label)
            if field_name is None:
                continue
            if field_name == "phone_numbers":
                if value:
                    phone_values.append(value)
            elif mapped.get(field_name) is None:
                mapped[field_name] = value or None

        if not mapped["name"]:
            mapped["name"] = _name_from_header(header_text)

        if not mapped["dealer_code"]:
            mapped["dealer_code"] = _dealer_code_from_header(header_text)

        dealer_code = normalize_dealer_code(mapped["dealer_code"])
        if not dealer_code:
            LOGGER.warning("Skipping table #%d without dealer code", table_index)
            continue

        record = DealershipRecord(
            dealer_code=dealer_code,
            name=mapped["name"],
            manager=mapped["manager"],
            province=mapped["province"],
            city=mapped["city"],
            address=mapped["address"],
            phone_numbers=clean_phone_numbers(*phone_values),
            agency_type=mapped["agency_type"],
            raw_row=raw_row,
        )
        records.append(record)

    return records


def dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
    """Drop exact duplicates while keeping split sale/service rows for one code."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[DealershipRecord] = []
    for record in records:
        key = (
            record.dealer_code or "",
            record.name or "",
            record.agency_type or "",
            record.address or "",
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def save_records(records: list[DealershipRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"[+] Saved {len(records)} dealer(s) to {output_path.resolve()}")


def crawl(output_path: Path) -> int:
    html, source_label = fetch_html()
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="ilia-dealer-table")
    records = parse_dealer_tables(soup)
    records = dedupe_records(records)

    if not records:
        print("[-] No dealer records parsed.")
        return 1

    save_records(records, output_path)

    print(
        f"[+] Summary: crawled {len(records)} dealer(s) from "
        f"{len(tables)} table(s) (source={source_label})"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl Ilia Auto dealer listings.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    _configure_logging(args.verbose)
    try:
        return crawl(args.output)
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
