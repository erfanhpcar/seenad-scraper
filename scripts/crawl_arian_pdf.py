#!/usr/bin/env python3
"""
Crawler: Arian Motor (آرین موتور) authorized dealer list from PDF.

Parses ``data/arian_motor_list_ameli.pdf`` — a digitized table where multiple
dealers are stacked inside single PDF row blocks (newline-separated cell values).

Usage:
  python scripts/crawl_arian_pdf.py
  python scripts/crawl_arian_pdf.py --output data/raw_arian_motor_dealers.json -v

Dependencies (project requirements.txt):
  pdfplumber
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

LOGGER = logging.getLogger("arian-pdf-crawler")

DEFAULT_PDF_PATH = Path("data/arian_motor_list_ameli.pdf")
DEFAULT_OUTPUT = Path("data/raw_arian_motor_dealers.json")

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
WS_RE = re.compile(r"\s+")

DEALER_CODE_RE = re.compile(r"^\d{3,4}$")
ROW_NUMBER_RE = re.compile(r"^\d{1,2}$")
AREA_CODE_RE = re.compile(r"^0\d{2}$")
TIME_RE = re.compile(r"\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}(?::\d{2})?)?|^\d{1,2}-\d{1,2}$")
PHONE_LOCAL_RE = re.compile(r"^[\d\-]{6,12}$")
PHONE_TOKEN_RE = re.compile(
    r"(?:0\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}|0\d{10}|09\d{9}|\d{7,8})"
)
ADDRESS_KEYWORDS = (
    "خیابان",
    "بلوار",
    "پلاک",
    "جنب",
    "کیلومتر",
    "کلاپ",
    "میدان",
    "جاده",
    "روبرو",
    "نبش",
    "کوچه",
    "اتوبان",
    "چهارراه",
    "مجتمع",
    "نمایشگاه",
    "تعمیرگاه",
)
AGENCY_KEYWORDS = ("گارانتی", "سرویس", "پوشش", "خودرو")
NAME_HINTS = ("خودرو", "نمایندگی", "موتور", "شرکت", "توسعه", "گستر", "تجارت", "پارس")
HEADER_MARKERS = ("کد عاملی", "نام عامل", "نام مدیر عامل", "ساعت کاری")
DATA_ROW_MIN_COLUMNS = 12

TYPO_MAP = {
    "اردبل": "اردبیل",
}

PROVINCES = {
    "آذربایجان شرقی",
    "آذربایجان غربی",
    "اردبیل",
    "اصفهان",
    "البرز",
    "ایلام",
    "بوشهر",
    "تهران",
    "چهارمحال و بختیاری",
    "خراسان جنوبی",
    "خراسان رضوی",
    "خراسان شمالی",
    "خوزستان",
    "زنجان",
    "سمنان",
    "سیستان و بلوچستان",
    "فارس",
    "قزوین",
    "قم",
    "کردستان",
    "کرمان",
    "کرمانشاه",
    "کهگیلویه و بویراحمد",
    "گلستان",
    "گیلان",
    "لرستان",
    "مازندران",
    "مرکزی",
    "هرمزگان",
    "همدان",
    "یزد",
}

CITIES = {
    "تبریز",
    "ارومیه",
    "اردبیل",
    "اصفهان",
    "کرج",
    "ایلام",
    "بوشهر",
    "تهران",
    "شهرکرد",
    "بیرجند",
    "مشهد",
    "بجنورد",
    "اهواز",
    "زنجان",
    "سمنان",
    "زاهدان",
    "شیراز",
    "قزوین",
    "قم",
    "سنندج",
    "کرمان",
    "کرمانشاه",
    "یاسوج",
    "گرگان",
    "رشت",
    "خرم‌آباد",
    "خرم آباد",
    "ساری",
    "اراک",
    "بندرعباس",
    "بندر عباس",
    "همدان",
    "یزد",
    "دزفول",
    "شهرضا",
    "کاشان",
    "نجف‌آباد",
    "نجف آباد",
    "ماهشهر",
    "آبادان",
    "شوشتر",
    "لاهیجان",
    "رامسر",
    "قائم‌شهر",
    "قائم شهر",
    "بابل",
    "آمل",
    "شاهرود",
    "سبزوار",
    "نیشابور",
    "مرودشت",
    "جهرم",
    "فسا",
    "داراب",
    "لار",
    "کازرون",
    "مراغه",
    "میانه",
    "مرند",
    "خوی",
    "مهاباد",
    "بوکان",
    "سقز",
    "مریوان",
    "ایلام",
    "دهلران",
    "شهرکرد",
    "بروجن",
    "شهرضا",
    "شهر ری",
    "ورامین",
    "اسلامشهر",
    "شهریار",
    "رباط‌کریم",
    "رباط کریم",
    "پردیس",
    "قم",
    "مق",
}


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
    source: str = "arian-motor.com"
    source_file: str = str(DEFAULT_PDF_PATH)
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


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\uf052", "").replace("\uf0a8", "").replace("\uf052", "")
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    if text in TYPO_MAP:
        text = TYPO_MAP[text]
    return text or None


def fix_mirrored_pdf_text(value: str | None) -> str | None:
    """
    This PDF encodes Persian RTL lines in reversed character order.

    Numeric/time-only fragments are left unchanged.
    """
    text = clean_text(value)
    if not text:
        return None
    if any("\u0600" <= char <= "\u06ff" for char in text):
        return text[::-1]
    return text


def _is_header_row(row_index: int, cells: list[str | None]) -> bool:
    if row_index < 2:
        return True
    joined = " ".join(clean_text(c) or "" for c in cells if c)
    fixed = fix_mirrored_pdf_text(joined) or joined
    return sum(1 for marker in HEADER_MARKERS if marker in fixed) >= 2


def _is_time_like(text: str) -> bool:
    return bool(TIME_RE.search(text))


def _is_phone_like(text: str) -> bool:
    if _is_time_like(text):
        return False
    compact = text.replace(" ", "")
    if not PHONE_LOCAL_RE.fullmatch(compact):
        return False
    digits = re.sub(r"\D", "", compact)
    return 7 <= len(digits) <= 11


def unstack_row(row: list[str | None]) -> list[list[str | None]]:
    """Split newline-stacked cells into aligned flat sub-rows."""
    cells = [cell if cell is not None else "" for cell in row]
    split_cells = [str(cell).split("\n") for cell in cells]
    max_lines = max((len(parts) for parts in split_cells), default=0)
    if max_lines <= 1:
        return [cells]

    padded_columns: list[list[str]] = []
    for parts in split_cells:
        padded_columns.append(parts + [""] * (max_lines - len(parts)))

    return [
        [padded_columns[col_index][line_index] for col_index in range(len(padded_columns))]
        for line_index in range(max_lines)
    ]


def clean_phone_numbers(*values: str | None, area_code: str | None = None) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = clean_text(value)
        if not text or text == "-":
            continue

        for chunk in re.split(r"[\s/،,]+", text):
            chunk = chunk.strip()
            if not chunk or chunk == "-":
                continue

            local_area = re.match(r"^(\d{7,8})\s*-\s*(0?\d{2,3})$", chunk)
            if local_area:
                chunk = f"{local_area.group(2)}{local_area.group(1)}"

            for match in PHONE_TOKEN_RE.findall(chunk):
                digits = re.sub(r"\D", "", match)
                if len(digits) < 7:
                    continue
                if len(digits) in {7, 8} and area_code:
                    ac = re.sub(r"\D", "", area_code)
                    if ac and not digits.startswith("0"):
                        digits = f"{ac}{digits}"
                if len(digits) >= 10 and digits not in seen:
                    seen.add(digits)
                    phones.append(digits)

    return phones


def _is_address(text: str) -> bool:
    return any(keyword in text for keyword in ADDRESS_KEYWORDS)


def _is_agency_type(text: str) -> bool:
    return any(keyword in text for keyword in AGENCY_KEYWORDS)


def _is_status(text: str) -> bool:
    return text in {"فعال", "-", "غیرفعال"}


def _is_name_like(text: str) -> bool:
    if _is_address(text) or _is_agency_type(text) or _is_status(text) or _is_time_like(text):
        return False
    if any(keyword in text for keyword in NAME_HINTS):
        return True
    return 3 <= len(text) <= 80 and bool(re.search(r"[\u0600-\u06ff]", text))


def _is_manager_like(text: str) -> bool:
    if _is_address(text) or _is_agency_type(text) or _is_status(text) or _is_time_like(text):
        return False
    words = text.split()
    return 2 <= len(words) <= 5 and bool(re.search(r"[\u0600-\u06ff]", text))


def _apply_positional_hints(
    cells: list[str | None],
    *,
    dealer_code: str | None,
    name: str | None,
    manager: str | None,
    province: str | None,
    city: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Use stable trailing columns when the row has the expected width."""
    normalized = [clean_text(fix_mirrored_pdf_text(cell)) for cell in cells]
    normalized = [text for text in normalized if text]
    if len(normalized) < DATA_ROW_MIN_COLUMNS:
        return dealer_code, name, manager, province, city

    tail = [clean_text(fix_mirrored_pdf_text(cell)) for cell in cells if cell is not None]
    tail = [text for text in tail if text]
    if len(tail) < 5:
        return dealer_code, name, manager, province, city

    row_num = tail[-1]
    prov = tail[-2]
    cty = tail[-3]
    code = tail[-4]
    agent_name = tail[-5]
    manager_name = tail[-6] if len(tail) >= 6 else None

    if ROW_NUMBER_RE.fullmatch(row_num or ""):
        if prov in PROVINCES:
            province = prov
        if cty in CITIES:
            city = cty
        elif cty in PROVINCES and province is None:
            province = cty
        if DEALER_CODE_RE.fullmatch(code or ""):
            dealer_code = code
        if agent_name and not _is_status(agent_name) and not ROW_NUMBER_RE.fullmatch(agent_name):
            name = agent_name
        if (
            manager_name
            and not _is_status(manager_name)
            and not ROW_NUMBER_RE.fullmatch(manager_name)
            and not _is_time_like(manager_name)
        ):
            manager = manager_name

    return dealer_code, name, manager, province, city


def map_flat_row(original_cells: list[str | None]) -> DealershipRecord | None:
    normalized_cells = [fix_mirrored_pdf_text(cell) for cell in original_cells]
    raw_row: dict[str, Any] = {"cells": [cell if cell is not None else "" for cell in original_cells]}

    dealer_code: str | None = None
    name: str | None = None
    manager: str | None = None
    province: str | None = None
    city: str | None = None
    address: str | None = None
    agency_type: str | None = None
    area_code: str | None = None
    phone_candidates: list[str] = []
    status_values: list[str] = []
    descriptive: list[str] = []

    for cell in normalized_cells:
        text = clean_text(cell)
        if not text or text == "-":
            continue

        if _is_time_like(text):
            raw_row.setdefault("schedule", []).append(text)
            continue

        if AREA_CODE_RE.fullmatch(text):
            area_code = text
            raw_row["area_code"] = area_code
            continue

        if DEALER_CODE_RE.fullmatch(text) and not ROW_NUMBER_RE.fullmatch(text):
            dealer_code = text
            continue

        if _is_status(text):
            status_values.append(text)
            continue

        if text in PROVINCES:
            province = text
            continue

        if text in CITIES:
            city = text
            continue

        if _is_address(text):
            address = f"{address} | {text}" if address else text
            continue

        if _is_phone_like(text):
            phone_candidates.append(text)
            continue

        if _is_agency_type(text):
            agency_type = f"{agency_type} | {text}" if agency_type else text
            continue

        descriptive.append(text)

    if status_values:
        raw_row["status"] = status_values

    for text in descriptive:
        if dealer_code and text == dealer_code:
            continue
        if ROW_NUMBER_RE.fullmatch(text):
            continue
        if name is None and _is_name_like(text):
            name = text
            continue
        if manager is None and _is_manager_like(text):
            manager = text
            continue
        if province is None and text in PROVINCES:
            province = text
            continue
        if city is None and text in CITIES:
            city = text
            continue
        if name is None:
            name = text
        elif manager is None and text != name:
            manager = text

    dealer_code, name, manager, province, city = _apply_positional_hints(
        original_cells,
        dealer_code=dealer_code,
        name=name,
        manager=manager,
        province=province,
        city=city,
    )

    phone_numbers = clean_phone_numbers(*phone_candidates, area_code=area_code)

    if manager and ROW_NUMBER_RE.fullmatch(manager):
        manager = None
    if manager and name and manager == name:
        manager = None
    if manager and manager in TYPO_MAP:
        city = city or TYPO_MAP[manager]
        manager = None

    if not dealer_code:
        return None

    if not name and not address:
        return None

    return DealershipRecord(
        dealer_code=dealer_code,
        name=name,
        manager=manager,
        province=province,
        city=city,
        address=address,
        phone_numbers=phone_numbers,
        agency_type=agency_type,
        raw_row=raw_row,
    )


def extract_records_from_pdf(pdf_path: Path) -> tuple[list[DealershipRecord], int, int]:
    records: list[DealershipRecord] = []
    table_count = 0
    row_blocks = 0

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables() or []
            table_count += len(tables)
            LOGGER.debug("Page %d: %d table(s)", page_index, len(tables))

            for table_index, table in enumerate(tables):
                for row_index, row in enumerate(table):
                    if not row or _is_header_row(row_index, row):
                        continue

                    row_blocks += 1
                    for sub_row_index, flat_row in enumerate(unstack_row(row)):
                        if sum(1 for cell in flat_row if clean_text(cell)) < 4:
                            continue
                        record = map_flat_row(flat_row)
                        if record is None:
                            LOGGER.debug(
                                "Skipped page=%d table=%d row=%d sub=%d (no dealer_code)",
                                page_index,
                                table_index,
                                row_index,
                                sub_row_index,
                            )
                            continue
                        record.raw_row["page"] = page_index
                        record.raw_row["table_index"] = table_index
                        record.raw_row["row_index"] = row_index
                        record.raw_row["sub_row_index"] = sub_row_index
                        records.append(record)

    return records, table_count, row_blocks


def dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
    best_by_code: dict[str, DealershipRecord] = {}

    def score(record: DealershipRecord) -> int:
        return sum(
            [
                bool(record.name),
                bool(record.manager),
                bool(record.address),
                bool(record.phone_numbers),
                bool(record.province),
                bool(record.city),
            ]
        )

    for record in records:
        key = record.dealer_code or ""
        if not key:
            continue
        existing = best_by_code.get(key)
        if existing is None or score(record) > score(existing):
            best_by_code[key] = record

    return list(best_by_code.values())


def save_records(records: list[DealershipRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def crawl(pdf_path: Path, output_path: Path) -> int:
    if not pdf_path.is_file():
        print(f"[!] PDF not found: {pdf_path.resolve()}")
        return 1

    records, table_count, row_blocks = extract_records_from_pdf(pdf_path)
    records = dedupe_records(records)

    if not records:
        print("[-] No dealer records extracted from PDF.")
        return 1

    save_records(records, output_path)
    print(
        f"[+] Extracted {len(records)} dealer record(s) from "
        f"{table_count} table(s) / {row_blocks} row block(s) in {pdf_path.name}"
    )
    print(f"[+] Saved to {output_path.resolve()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse Arian Motor dealer PDF list.")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_PDF_PATH,
        help=f"Input PDF path (default: {DEFAULT_PDF_PATH})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    _configure_logging(args.verbose)
    return crawl(args.pdf, args.output)


if __name__ == "__main__":
    sys.exit(main())
