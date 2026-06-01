#!/usr/bin/env python3
"""
Crawler: Arian Motor (آرین موتور) authorized Mitsubishi / Lamari dealership branches.

Fetches all branches via WordPress admin-ajax.php (search_agency bulk listing),
optionally enriches province from per-province list_agency calls, and writes
structured JSON/JSONL.

Usage:
  python scripts/crawl_arian_motor.py
  python scripts/crawl_arian_motor.py --format table
  python scripts/crawl_arian_motor.py --output data/raw_arian_motor_dealers.json -v
  python scripts/crawl_arian_motor.py --no-province-enrichment

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
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException

LOGGER = logging.getLogger("arian-motor-crawler")

BRANCH_PAGE_URL = "https://arian-motor.com/branch/"
ADMIN_AJAX_URL = "https://arian-motor.com/wp-admin/admin-ajax.php"
DEFAULT_OUTPUT = Path("data/raw_arian_motor_dealers.json")
DEFAULT_AGENCY_TYPE = "1"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DOCUMENT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
}

AJAX_HEADERS = {
    **DOCUMENT_HEADERS,
    "Referer": BRANCH_PAGE_URL,
    "Origin": "https://arian-motor.com",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
CODE_RE = re.compile(r"کد\s*(\d+)", re.I)
TRAILING_CODE_RE = re.compile(r"(?:نمایندگی\s+)?(.+?)\s+(\d{3,5})\s*$")
PHONE_RE = re.compile(
    r"(?:0\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}|0\d{10}|09\d{9})"
)
CITY_FROM_ADDRESS_RE = re.compile(
    r"(?:فروش|خدمات\s*پس\s*از\s*فروش)\s*[:：]\s*([^،,\n<]+)",
    re.I,
)

LABEL_COMPANY = "نام شرکت"
LABEL_DEALER_TYPE = "نوع نمایندگی"
LABEL_REPRESENTATIVE = "نام نماینده"
LABEL_PHONE = "شماره تلفن"
LABEL_ADDRESS = "آدرس"
LABEL_ACTIVITY = "حوزه های فعالیت"


@dataclass
class DealershipRecord:
    name: str | None
    code: str | None
    city: str | None
    province: str | None
    address: str | None
    phone_numbers: list[str] = field(default_factory=list)
    brand_focus: list[str] = field(default_factory=list)
    representative: str | None = None
    activity_areas: list[str] = field(default_factory=list)
    dealer_type_label: str | None = None
    source: str = "arian-motor.com"
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


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
    text = re.sub(r"[:：]\s*$", "", text).strip()
    return text or None


def normalize_phones(*chunks: str | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        normalized_chunk = clean_farsi_text(chunk) or ""
        for match in PHONE_RE.findall(normalized_chunk):
            phone = re.sub(r"[\s\-]", "", match)
            if phone in seen:
                continue
            seen.add(phone)
            result.append(phone)
    return result


def infer_brand_focus(
    dealer_type_label: str | None,
    block: Tag,
) -> list[str]:
    brands: list[str] = []
    haystack = " ".join(
        part
        for part in (
            dealer_type_label or "",
            " ".join(block.get("class", [])),
            block.get_text(" ", strip=True),
        )
        if part
    ).lower()

    if "میتسوبیشی" in haystack or "mitsubishi" in haystack:
        brands.append("Mitsubishi")
    if "لاماری" in haystack or "lamari" in haystack:
        brands.append("Lamari")

    if not brands and dealer_type_label:
        if "میتسوبیشی" in dealer_type_label:
            brands.append("Mitsubishi")
        elif "لاماری" in dealer_type_label:
            brands.append("Lamari")

    return brands


def extract_code(company_name: str | None) -> str | None:
    if not company_name:
        return None
    match = CODE_RE.search(company_name)
    if match:
        return match.group(1)
    tail_match = TRAILING_CODE_RE.search(company_name.strip())
    if tail_match:
        return tail_match.group(2)
    return None


def extract_city_from_address(address: str | None) -> str | None:
    if not address:
        return None
    cities: list[str] = []
    for match in CITY_FROM_ADDRESS_RE.finditer(address):
        candidate = clean_farsi_text(match.group(1))
        if not candidate:
            continue
        candidate = re.sub(
            r"^(?:فروش|خدمات\s*پس\s*از\s*فروش)\s*[:：]?\s*",
            "",
            candidate,
            flags=re.I,
        ).strip()
        candidate = clean_farsi_text(candidate)
        if candidate and candidate not in cities:
            cities.append(candidate)
    return cities[0] if cities else None


def _label_value_pairs(block: Tag) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for ul in block.select("ul.agency_ul"):
        label_el = ul.find("li", class_="agency_lable")
        value_el = ul.find("li", class_="agency_daron")
        if not label_el or not value_el:
            continue
        label = clean_farsi_text(label_el.get_text(strip=True))
        if not label or "اطلاعات بیشتر" in label:
            continue
        label = label.rstrip(":").strip()
        value = clean_farsi_text(value_el.get_text("\n", strip=True))
        if label and value:
            pairs[label] = value
    return pairs


def parse_activity_areas(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,،\n]+", raw)
    return [clean_farsi_text(part) or "" for part in parts if clean_farsi_text(part)]


def parse_dealership_block(
    block: Tag,
    *,
    province: str | None = None,
) -> DealershipRecord | None:
    pairs = _label_value_pairs(block)

    company = pairs.get(LABEL_COMPANY) or pairs.get("نام شرکت")
    if not company:
        return None

    dealer_type_label = pairs.get(LABEL_DEALER_TYPE) or pairs.get("نوع نمایندگی")
    address = pairs.get(LABEL_ADDRESS) or pairs.get("آدرس")
    phone_field = pairs.get(LABEL_PHONE) or pairs.get("شماره تلفن")
    representative = pairs.get(LABEL_REPRESENTATIVE) or pairs.get("نام نماینده")
    activity_raw = pairs.get(LABEL_ACTIVITY) or pairs.get("حوزه های فعالیت")

    code = extract_code(company)
    city = extract_city_from_address(address)

    activity_areas: list[str] = []
    brands_ul = block.select_one("ul.agency_brands")
    if brands_ul:
        activity_areas = [
            clean_farsi_text(li.get_text(strip=True)) or ""
            for li in brands_ul.find_all("li")
            if clean_farsi_text(li.get_text(strip=True))
        ]
    if not activity_areas:
        activity_areas = parse_activity_areas(activity_raw)

    return DealershipRecord(
        name=clean_farsi_text(company),
        code=code,
        city=city,
        province=clean_farsi_text(province),
        address=address,
        phone_numbers=normalize_phones(phone_field, address),
        brand_focus=infer_brand_focus(dealer_type_label, block),
        representative=representative,
        activity_areas=activity_areas,
        dealer_type_label=dealer_type_label,
    )


def parse_dealership_html(
    html: str,
    *,
    province: str | None = None,
) -> list[DealershipRecord]:
    if not html or "no-agency" in html.lower():
        return []

    soup = BeautifulSoup(html, "html.parser")
    records: list[DealershipRecord] = []
    for block in soup.select(".agency_self"):
        record = parse_dealership_block(block, province=province)
        if record:
            records.append(record)
    return records


class ArianMotorCrawler:
    def __init__(
        self,
        *,
        agency_type: str = DEFAULT_AGENCY_TYPE,
        timeout: float = 30.0,
        enrich_provinces: bool = True,
        province_delay: float = 0.35,
    ) -> None:
        self.agency_type = agency_type
        self.timeout = timeout
        self.enrich_provinces = enrich_provinces
        self.province_delay = province_delay

        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update(DOCUMENT_HEADERS)

    def _post_ajax(self, payload: dict[str, str]) -> str:
        response = self._session.post(
            ADMIN_AJAX_URL,
            data=payload,
            headers=AJAX_HEADERS,
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def fetch_branch_page(self) -> str:
        response = self._session.get(BRANCH_PAGE_URL, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def fetch_all_dealerships_html(self) -> str:
        """Bulk listing: empty name search returns every branch in one response."""
        payload = {
            "action": "search_agency",
            "type_search": "name",
            "search_text": "",
            "type": self.agency_type,
        }
        LOGGER.info("POST admin-ajax.php action=search_agency (bulk name search)")
        html = self._post_ajax(payload)
        if "no-agency" in html.lower():
            raise RuntimeError("Bulk search_agency returned no dealerships.")
        return html

    def load_provinces(self, branch_html: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(branch_html, "html.parser")
        seen: set[str] = set()
        provinces: list[tuple[str, str]] = []
        for anchor in soup.select("a.ag_btn_link"):
            province_id = (anchor.get("id") or "").strip()
            province_name = clean_farsi_text(
                anchor.get("title") or anchor.get_text(strip=True)
            )
            if not province_id or not province_name or province_id in seen:
                continue
            seen.add(province_id)
            provinces.append((province_id, province_name))
        return provinces

    def build_province_code_map(self, provinces: list[tuple[str, str]]) -> dict[str, str]:
        code_to_province: dict[str, str] = {}
        for province_id, province_name in provinces:
            payload = {
                "action": "list_agency",
                "id": province_name,
                "ostani": province_id,
                "type": self.agency_type,
            }
            try:
                html = self._post_ajax(payload)
            except RequestException as exc:
                LOGGER.warning("list_agency failed for %s: %s", province_name, exc)
                continue

            for record in parse_dealership_html(html, province=province_name):
                if record.code and record.code not in code_to_province:
                    code_to_province[record.code] = province_name

            time.sleep(self.province_delay)

        return code_to_province

    def crawl(self) -> list[DealershipRecord]:
        bulk_html = self.fetch_all_dealerships_html()
        records = parse_dealership_html(bulk_html)
        LOGGER.info("Parsed %d dealership blocks from bulk search", len(records))

        if not self.enrich_provinces:
            return self._dedupe_records(records)

        branch_html = self.fetch_branch_page()
        provinces = self.load_provinces(branch_html)
        LOGGER.info("Enriching province for %d map provinces", len(provinces))
        code_to_province = self.build_province_code_map(provinces)

        enriched: list[DealershipRecord] = []
        for record in records:
            province = record.province
            if not province and record.code:
                province = code_to_province.get(record.code)
            enriched.append(
                DealershipRecord(
                    name=record.name,
                    code=record.code,
                    city=record.city,
                    province=province,
                    address=record.address,
                    phone_numbers=record.phone_numbers,
                    brand_focus=record.brand_focus,
                    representative=record.representative,
                    activity_areas=record.activity_areas,
                    dealer_type_label=record.dealer_type_label,
                    source=record.source,
                    scraped_at=record.scraped_at,
                )
            )

        return self._dedupe_records(enriched)

    @staticmethod
    def _dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
        seen: set[str] = set()
        unique: list[DealershipRecord] = []
        for record in records:
            key = record.code or (record.name or "", record.address or "")
            key_str = str(key)
            if key_str in seen:
                continue
            seen.add(key_str)
            unique.append(record)
        unique.sort(key=lambda row: (row.province or "", row.city or "", row.name or ""))
        return unique


def print_table(records: list[DealershipRecord]) -> None:
    if not records:
        print("No dealerships extracted.")
        return

    headers = ("code", "name", "province", "city", "phones", "brand_focus")
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.code or "-",
                (record.name or "-")[:42],
                (record.province or "-")[:14],
                (record.city or "-")[:18],
                ", ".join(record.phone_numbers[:2]) or "-",
                ", ".join(record.brand_focus) or "-",
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(list(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt_row(row))
    print()
    print(f"Total: {len(records)} dealerships")


def write_json(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Arian Motor dealership branches via WordPress admin-ajax."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Write JSON array to this path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Optional JSONL output path (one record per line)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "both"),
        default="both",
        help="Console output format (default: both table and JSON summary)",
    )
    parser.add_argument(
        "--agency-type",
        default=DEFAULT_AGENCY_TYPE,
        help="WordPress agency plugin type filter (default: 1 = Mitsubishi network)",
    )
    parser.add_argument(
        "--no-province-enrichment",
        action="store_true",
        help="Skip per-province list_agency calls (faster; province may be empty)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--province-delay",
        type=float,
        default=0.35,
        help="Delay between province list_agency requests",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _configure_logging(args.verbose)

    crawler = ArianMotorCrawler(
        agency_type=args.agency_type,
        timeout=args.timeout,
        enrich_provinces=not args.no_province_enrichment,
        province_delay=args.province_delay,
    )

    try:
        records = crawler.crawl()
    except (RequestException, RuntimeError) as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    LOGGER.info("Wrote %d records to %s", len(records), args.output)

    if args.jsonl:
        write_jsonl(args.jsonl, records)
        LOGGER.info("Wrote JSONL to %s", args.jsonl)

    if args.format in ("table", "both"):
        print_table(records)

    if args.format in ("json", "both"):
        print(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
