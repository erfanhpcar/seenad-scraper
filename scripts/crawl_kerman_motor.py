#!/usr/bin/env python3
"""
Crawler: Kerman Motor (کرمان موتور) authorized dealership network.

The public index at /dealers/1/ is a filter shell without listing cards. Listings
render as .news-block-two cards on URLs shaped like:

  /dealers/{page}/{province_id}/{dealer_code}/{service_type}/{ev_flag}

This crawler loads provinces from the index, then paginates each province until
a 404 or a page with no dealership blocks.

Usage:
  python scripts/crawl_kerman_motor.py
  python scripts/crawl_kerman_motor.py --output data/raw_kerman_motor_dealers.json -v
  python scripts/crawl_kerman_motor.py --format table

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
import requests
import urllib3
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.exceptions import RequestException

LOGGER = logging.getLogger("kerman-motor-crawler")

BASE_URL = "https://kermanmotor.com"
INDEX_URL = f"{BASE_URL}/dealers/1/"
DEFAULT_OUTPUT = Path("data/raw_kerman_motor_dealers.json")
DEFAULT_SERVICE_TYPE = "3"  # sale + after-sale (matches site filter "all")
DEFAULT_DEALER_FILTER = "-1"  # all dealerships in province
DEFAULT_EV_FLAG = "0"
PAGE_DELAY_S = 0.5

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DOCUMENT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
}

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
DEALER_CODE_RE = re.compile(r"شماره\s*(\d+)", re.I)
PHONE_RE = re.compile(
    r"(?:0\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}|0\d{10}|09\d{9})"
)
LOCAL_WITH_AREA_RE = re.compile(
    r"(?P<locals>[\d\-]+)\s*-\s*(?P<area>0\d{2})\s*$"
)
CITY_FROM_ADDRESS_RE = re.compile(r"^([^،,]+)")

LABEL_ADDRESS = "آدرس"
LABEL_ADDRESS_SALE = "آدرس فروش"
LABEL_ADDRESS_AFTERSALE = "آدرس خدمات پس از فروش"
LABEL_MANAGEMENT = "مدیریت"
LABEL_PHONE_SALE = "تلفن فروش"
LABEL_PHONE_AFTERSALE = "تلفن خدمات پس از فروش"
LABEL_PHONE_TOWING = "تلفن امداد سیار"

ACTIVITY_SALE = "فروش"
ACTIVITY_AFTERSALE = "خدمات پس از فروش"
ACTIVITY_TOWING = "امداد سیار"
ACTIVITY_EV = "ایستگاه شارژ"

ICON_ACTIVITY_MAP = {
    "fa-sale": ACTIVITY_SALE,
    "fa-aftersale": ACTIVITY_AFTERSALE,
    "fa-towing": ACTIVITY_TOWING,
    "fa-ev": ACTIVITY_EV,
}


@dataclass
class DealershipRecord:
    name: str | None
    code: str | None
    city: str | None
    province: str | None
    address: str | None
    address_sale: str | None = None
    address_aftersale: str | None = None
    phone_numbers: list[str] = field(default_factory=list)
    phone_sale: list[str] = field(default_factory=list)
    phone_aftersale: list[str] = field(default_factory=list)
    phone_towing: list[str] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    management: str | None = None
    source_page: int | None = None
    province_id: str | None = None
    source_url: str | None = None
    source: str = "kermanmotor.com"
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


def _expand_phone_token(token: str) -> list[str]:
    phones: list[str] = []
    normalized = clean_farsi_text(token) or ""
    if not normalized:
        return phones

    for segment in re.split(r"[,،]+", normalized):
        segment = segment.strip()
        if not segment:
            continue

        area_suffix = LOCAL_WITH_AREA_RE.search(segment)
        if area_suffix:
            area_digits = re.sub(r"\D", "", area_suffix.group("area"))
            for local_part in area_suffix.group("locals").split("-"):
                local_digits = re.sub(r"\D", "", local_part)
                if len(local_digits) >= 4 and area_digits:
                    phones.append(f"{area_digits}{local_digits}")
            continue

        for match in PHONE_RE.findall(segment):
            phone = re.sub(r"\D", "", match)
            if phone:
                phones.append(phone)

    return phones


def normalize_phones(*chunks: str | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        for phone in _expand_phone_token(chunk):
            if phone in seen:
                continue
            seen.add(phone)
            result.append(phone)
    return result


def extract_dealer_code(title: str | None) -> str | None:
    if not title:
        return None
    match = DEALER_CODE_RE.search(title)
    return match.group(1) if match else None


def extract_city_from_address(address: str | None) -> str | None:
    if not address:
        return None
    match = CITY_FROM_ADDRESS_RE.match(address.strip())
    if not match:
        return None
    return clean_farsi_text(match.group(1))


def infer_activity_from_icons(block: Tag) -> list[str]:
    activities: list[str] = []
    for icon in block.select("i.fa"):
        classes = icon.get("class") or []
        for css_class, label in ICON_ACTIVITY_MAP.items():
            if css_class in classes and label not in activities:
                activities.append(label)
    return activities


def infer_brands_from_icons(block: Tag) -> list[str]:
    brands: list[str] = []
    for icon in block.select("i.fa[title]"):
        title = clean_farsi_text(icon.get("title"))
        if title and title not in brands and title not in ICON_ACTIVITY_MAP.values():
            brands.append(title)
    return brands


def _append_field(fields: dict[str, str], label: str, value: str) -> None:
    label = label.strip()
    value = value.strip()
    if not label or not value:
        return
    existing = fields.get(label)
    if existing:
        fields[label] = f"{existing} | {value}"
    else:
        fields[label] = value


def _normalize_label(raw_label: str | None) -> str | None:
    if not raw_label:
        return None
    label = clean_farsi_text(raw_label) or ""
    return label.rstrip(":").strip() or None


def parse_labeled_paragraph(paragraph: Tag | None) -> dict[str, str]:
    if paragraph is None:
        return {}

    fields: dict[str, str] = {}
    current_label: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_label, buffer
        if current_label and buffer:
            _append_field(fields, current_label, " ".join(buffer))
        buffer = []

    def absorb_text(text: str | None) -> None:
        cleaned = clean_farsi_text(text)
        if cleaned:
            buffer.append(cleaned)

    def walk(node: Tag | NavigableString) -> None:
        nonlocal current_label

        if isinstance(node, NavigableString):
            absorb_text(str(node))
            return

        if node.name == "b":
            flush()
            current_label = _normalize_label(node.get_text(strip=True))
            return

        if node.name == "br":
            flush()
            return

        if node.name == "span":
            leading_label = node.find("b", recursive=False)
            if leading_label is not None:
                flush()
                current_label = _normalize_label(leading_label.get_text(strip=True))
                remainder = leading_label.next_sibling
                while remainder is not None:
                    if isinstance(remainder, NavigableString):
                        absorb_text(str(remainder))
                    elif isinstance(remainder, Tag):
                        if remainder.name == "b":
                            break
                        if remainder.name == "br":
                            break
                        walk(remainder)
                    remainder = remainder.next_sibling
                return

            if "phone-direction" in (node.get("class") or []):
                absorb_text(node.get_text(" ", strip=True))
                return

        for child in node.children:
            walk(child)

    for child in paragraph.children:
        walk(child)
    flush()
    return fields


def _primary_address(fields: dict[str, str]) -> str | None:
    for key in (LABEL_ADDRESS, LABEL_ADDRESS_SALE, LABEL_ADDRESS_AFTERSALE):
        value = fields.get(key)
        if value:
            return value
    return None


def parse_dealership_block(
    block: Tag,
    *,
    province_name: str | None,
    province_id: str | None,
    page_number: int,
    source_url: str,
) -> DealershipRecord | None:
    title_el = block.select_one("h3")
    title = clean_farsi_text(title_el.get_text(strip=True) if title_el else None)
    code = extract_dealer_code(title)

    paragraph = block.select_one(".content p") or block.select_one("p")
    fields = parse_labeled_paragraph(paragraph)

    management = fields.get(LABEL_MANAGEMENT)
    address = _primary_address(fields)
    address_sale = fields.get(LABEL_ADDRESS_SALE)
    address_aftersale = fields.get(LABEL_ADDRESS_AFTERSALE)

    phone_sale = normalize_phones(fields.get(LABEL_PHONE_SALE))
    phone_aftersale = normalize_phones(fields.get(LABEL_PHONE_AFTERSALE))
    phone_towing = normalize_phones(fields.get(LABEL_PHONE_TOWING))
    all_phones = normalize_phones(
        fields.get(LABEL_PHONE_SALE),
        fields.get(LABEL_PHONE_AFTERSALE),
        fields.get(LABEL_PHONE_TOWING),
        paragraph.get_text(" ", strip=True) if paragraph else None,
    )

    city = extract_city_from_address(
        address_sale or address_aftersale or address
    )
    activity = infer_activity_from_icons(block)
    if not activity:
        if phone_sale or address_sale:
            activity.append(ACTIVITY_SALE)
        if phone_aftersale or address_aftersale:
            activity.append(ACTIVITY_AFTERSALE)

    return DealershipRecord(
        name=management or title,
        code=code,
        city=city,
        province=clean_farsi_text(province_name),
        address=address,
        address_sale=address_sale,
        address_aftersale=address_aftersale,
        phone_numbers=all_phones,
        phone_sale=phone_sale,
        phone_aftersale=phone_aftersale,
        phone_towing=phone_towing,
        activity=activity,
        brands=infer_brands_from_icons(block),
        management=management,
        source_page=page_number,
        province_id=province_id,
        source_url=source_url,
    )


def parse_dealership_html(
    html: str,
    *,
    province_name: str | None,
    province_id: str | None,
    page_number: int,
    source_url: str,
) -> list[DealershipRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[DealershipRecord] = []
    for block in soup.select(".news-block-two"):
        record = parse_dealership_block(
            block,
            province_name=province_name,
            province_id=province_id,
            page_number=page_number,
            source_url=source_url,
        )
        if record and (record.code or record.name):
            records.append(record)
    return records


def build_list_url(
    page: int,
    province_id: str,
    *,
    dealer_code: str = DEFAULT_DEALER_FILTER,
    service_type: str = DEFAULT_SERVICE_TYPE,
    ev_flag: str = DEFAULT_EV_FLAG,
) -> str:
    return (
        f"{BASE_URL}/dealers/{page}/{province_id}/"
        f"{dealer_code}/{service_type}/{ev_flag}"
    )


class KermanMotorCrawler:
    def __init__(
        self,
        *,
        timeout: float = 60.0,
        page_delay: float = PAGE_DELAY_S,
        service_type: str = DEFAULT_SERVICE_TYPE,
    ) -> None:
        self.timeout = timeout
        self.page_delay = page_delay
        self.service_type = service_type

        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update(DOCUMENT_HEADERS)

    def fetch_html(self, url: str) -> tuple[int, str]:
        response = self._session.get(url, timeout=self.timeout)
        # Site serves UTF-8; chardet apparent_encoding often mis-detects Farsi HTML.
        response.encoding = "utf-8"
        return response.status_code, response.text

    def load_provinces(self) -> list[tuple[str, str]]:
        status, html = self.fetch_html(INDEX_URL)
        if status != 200:
            raise RuntimeError(f"Failed to load index {INDEX_URL} (HTTP {status})")

        soup = BeautifulSoup(html, "html.parser")
        provinces: list[tuple[str, str]] = []
        seen: set[str] = set()
        for option in soup.select("#filterProvince option"):
            province_id = (option.get("value") or "").strip()
            province_name = clean_farsi_text(option.get_text(strip=True))
            if not province_id or not province_name or province_id in seen:
                continue
            seen.add(province_id)
            provinces.append((province_id, province_name))

        if not provinces:
            raise RuntimeError("No provinces found on dealer index page.")
        return provinces

    def crawl_province(
        self,
        province_id: str,
        province_name: str,
    ) -> list[DealershipRecord]:
        records: list[DealershipRecord] = []
        page = 1

        while True:
            url = build_list_url(
                page,
                province_id,
                service_type=self.service_type,
            )
            LOGGER.debug("GET %s", url)
            status, html = self.fetch_html(url)

            if status == 404:
                LOGGER.debug(
                    "Province %s page %s: HTTP 404, stopping pagination",
                    province_name,
                    page,
                )
                break

            if status != 200:
                LOGGER.warning(
                    "Province %s page %s: HTTP %s, stopping pagination",
                    province_name,
                    page,
                    status,
                )
                break

            page_records = parse_dealership_html(
                html,
                province_name=province_name,
                province_id=province_id,
                page_number=page,
                source_url=url,
            )
            if not page_records:
                LOGGER.debug(
                    "Province %s page %s: no dealership blocks, stopping",
                    province_name,
                    page,
                )
                break

            records.extend(page_records)
            LOGGER.info(
                "Province %s (%s) page %s: %s dealerships",
                province_name,
                province_id,
                page,
                len(page_records),
            )

            page += 1
            time.sleep(self.page_delay)

        return records

    def crawl_bare_pages(self, *, max_empty_pages: int = 2) -> list[DealershipRecord]:
        """Probe /dealers/{n}/ without province filters (usually empty after page 1)."""
        records: list[DealershipRecord] = []
        page = 1
        empty_streak = 0

        while empty_streak < max_empty_pages:
            url = f"{BASE_URL}/dealers/{page}/"
            status, html = self.fetch_html(url)

            if status == 404:
                break

            page_records = parse_dealership_html(
                html,
                province_name=None,
                province_id=None,
                page_number=page,
                source_url=url,
            )
            if not page_records:
                empty_streak += 1
            else:
                empty_streak = 0
                records.extend(page_records)
                LOGGER.info("Bare page %s: %s dealerships", page, len(page_records))

            page += 1
            time.sleep(self.page_delay)

        return records

    def crawl(self) -> list[DealershipRecord]:
        all_records: list[DealershipRecord] = []

        bare_records = self.crawl_bare_pages(max_empty_pages=1)
        if bare_records:
            all_records.extend(bare_records)
        else:
            LOGGER.info(
                "Index pages have no listing cards; crawling per-province listings"
            )

        provinces = self.load_provinces()
        LOGGER.info("Loaded %s provinces from index", len(provinces))

        for province_id, province_name in provinces:
            province_records = self.crawl_province(province_id, province_name)
            all_records.extend(province_records)
            time.sleep(self.page_delay)

        return self._dedupe_records(all_records)

    @staticmethod
    def _dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
        seen: set[str] = set()
        unique: list[DealershipRecord] = []
        for record in records:
            key = record.code or f"{record.name}|{record.address}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(record)
        unique.sort(
            key=lambda row: (
                row.province or "",
                row.city or "",
                row.code or "",
                row.name or "",
            )
        )
        return unique


def print_table(records: list[DealershipRecord]) -> None:
    if not records:
        print("No dealerships extracted.")
        return

    headers = ("code", "name", "province", "city", "phones", "activity")
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.code or "-",
                (record.name or "-")[:36],
                (record.province or "-")[:14],
                (record.city or "-")[:16],
                ", ".join(record.phone_numbers[:2]) or "-",
                ", ".join(record.activity) or "-",
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Kerman Motor authorized dealership listings."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Write JSON array to this path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "both"),
        default="both",
        help="Console output format (default: both)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=PAGE_DELAY_S,
        help="Delay between HTTP requests in seconds",
    )
    parser.add_argument(
        "--service-type",
        default=DEFAULT_SERVICE_TYPE,
        help="Service filter segment (1=sale, 2=after-sale, 3=all)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _configure_logging(args.verbose)

    crawler = KermanMotorCrawler(
        timeout=args.timeout,
        page_delay=args.page_delay,
        service_type=args.service_type,
    )

    try:
        records = crawler.crawl()
    except (RequestException, RuntimeError) as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    LOGGER.info("Wrote %d records to %s", len(records), args.output)

    if args.format in ("table", "both"):
        print_table(records)

    if args.format in ("json", "both"):
        print(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
