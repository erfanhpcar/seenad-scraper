#!/usr/bin/env python3
"""
Crawler: BAMCO / Bahman Car Mfg (خودروسازان بم) authorized dealer list from PDF.

Parses ``data/bahman_dealers.pdf``. Persian text in table cells is character-reversed
(RTL extraction artefact); digits and phone fragments stay forward-facing.

Usage (Docker backend):
  docker exec seenad_backend python3 scripts/crawl_bamco_pdf.py
  docker exec seenad_backend python3 scripts/crawl_bamco_pdf.py -v

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

LOGGER = logging.getLogger("bamco-pdf-crawler")

DEFAULT_PDF_PATH = Path("data/bahman_dealers.pdf")
DEFAULT_OUTPUT = Path("data/raw_bamco_dealers.json")

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
WS_RE = re.compile(r"\s+")
PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06ff]")
DEALER_CODE_RE = re.compile(r"^\d{3,5}$")
ROW_NUMBER_RE = re.compile(r"^\d{1,3}$")
PHONE_CHUNK_RE = re.compile(r"[\d\-]+")

# Logical column indices after reversing pdfplumber's mirrored column order.
COL_ROW = 0
COL_NAME = 1
COL_DEALER_CODE = 2
COL_PROVINCE = 3
COL_CITY = 4
COL_AGENCY_TYPE = 5
COL_ADDRESS = 6
COL_CITY_CODE = 7
COL_SALES_PHONE = 8
COL_SERVICE_PHONE = 9
EXPECTED_COLUMNS = 10


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
    source: str = "bamco.ir"
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
    text = str(value).replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def fix_rtl_cell_text(value: str | None) -> str | None:
    """
    Correct RTL-mirrored Persian while preserving numeric / phone tokens.

    1. Split on whitespace into tokens.
    2. Reverse token order.
    3. Reverse characters inside tokens that contain Persian letters.
    4. Leave numeric / dash phone tokens unchanged.
    """
    text = clean_text(value)
    if not text:
        return None

    corrected_lines: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        tokens.reverse()
        fixed_tokens: list[str] = []
        for token in tokens:
            if PERSIAN_CHAR_RE.search(token):
                fixed_tokens.append(token[::-1])
            else:
                fixed_tokens.append(token)
        corrected_lines.append(" ".join(fixed_tokens))

    return " | ".join(corrected_lines) if len(corrected_lines) > 1 else (
        corrected_lines[0] if corrected_lines else None
    )


def normalize_city_code(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if len(digits) == 2:
        return f"0{digits}"
    if len(digits) == 3 and digits.startswith("0"):
        return digits
    if len(digits) == 3:
        return f"0{digits}"
    return digits


def _phone_chunks(value: str | None) -> list[str]:
    if not value:
        return []
    chunks: list[str] = []
    for line in value.replace("|", "\n").split("\n"):
        for part in re.split(r"[,،/]+", line):
            part = part.strip()
            if part and part != "-":
                chunks.append(part)
    return chunks


def normalize_phone_chunk(chunk: str, city_code: str | None) -> list[str]:
    chunk = clean_text(chunk) or ""
    if not chunk or chunk == "-":
        return []

    phones: list[str] = []
    for match in PHONE_CHUNK_RE.findall(chunk):
        match = match.strip("-")
        if not match:
            continue

        local_area = re.match(r"^(\d{7,8})-(\d{1,3})$", match)
        if local_area:
            local = local_area.group(1)
            if city_code:
                phones.append(f"{city_code}{local}")
            else:
                area = normalize_city_code(local_area.group(2))
                if area:
                    phones.append(f"{area}{local}")
            continue

        digits = re.sub(r"\D", "", match)
        if not digits:
            continue

        if digits.startswith("09") and len(digits) == 11:
            phones.append(digits)
            continue

        if digits.startswith("0") and len(digits) >= 10:
            phones.append(digits)
            continue

        if len(digits) in {7, 8} and city_code:
            phones.append(f"{city_code}{digits}")
            continue

        if len(digits) >= 10:
            phones.append(digits)

    return phones


def clean_phone_numbers(
    sales_phone: str | None,
    service_phone: str | None,
    city_code: str | None,
) -> list[str]:
    area = normalize_city_code(city_code)
    seen: set[str] = set()
    phones: list[str] = []

    for chunk in _phone_chunks(sales_phone) + _phone_chunks(service_phone):
        for candidate in normalize_phone_chunk(chunk, area):
            if candidate not in seen:
                seen.add(candidate)
                phones.append(candidate)

    return phones


def _is_title_or_header_row(cells: list[str | None]) -> bool:
    joined = " ".join(c for c in cells if c)
    if not joined.strip():
        return True
    fixed = fix_rtl_cell_text(joined) or joined
    header_hints = ("ردیف", "نام عامل", "کد", "استان", "آدرس", "تلفن")
    if sum(1 for hint in header_hints if hint in fixed) >= 3:
        return True
    title_hints = ("نمایندگی", "شرکت خودروسازان")
    return any(hint in fixed for hint in title_hints) and not any(
        DEALER_CODE_RE.fullmatch(re.sub(r"\D", "", c or "")) for c in cells if c
    )


def _logical_columns(raw_row: list[str | None]) -> list[str | None]:
    """pdfplumber returns columns right-to-left; reverse to logical order."""
    cells = list(raw_row)
    if len(cells) == EXPECTED_COLUMNS:
        cells.reverse()
    elif len(cells) > EXPECTED_COLUMNS:
        cells = cells[-EXPECTED_COLUMNS:]
        cells.reverse()
    else:
        cells = [None] * (EXPECTED_COLUMNS - len(cells)) + cells
        cells.reverse()
    return cells


def _cell_at(cells: list[str | None], index: int) -> str | None:
    if index < 0 or index >= len(cells):
        return None
    return fix_rtl_cell_text(cells[index])


def parse_data_row(
    raw_cells: list[str | None],
    *,
    page_index: int,
    row_index: int,
) -> DealershipRecord | None:
    logical = _logical_columns(raw_cells)
    if len(logical) < EXPECTED_COLUMNS:
        logical.extend([None] * (EXPECTED_COLUMNS - len(logical)))

    row_number = _cell_at(logical, COL_ROW)
    name = _cell_at(logical, COL_NAME)
    dealer_code = clean_text(logical[COL_DEALER_CODE])
    province = _cell_at(logical, COL_PROVINCE)
    city = _cell_at(logical, COL_CITY)
    agency_type = _cell_at(logical, COL_AGENCY_TYPE)
    address = _cell_at(logical, COL_ADDRESS)
    city_code = _cell_at(logical, COL_CITY_CODE)
    sales_phone = clean_text(logical[COL_SALES_PHONE])
    service_phone = clean_text(logical[COL_SERVICE_PHONE])

    if dealer_code:
        dealer_code = re.sub(r"\D", "", dealer_code) or dealer_code

    if not dealer_code or not DEALER_CODE_RE.fullmatch(dealer_code):
        return None

    phone_numbers = clean_phone_numbers(sales_phone, service_phone, city_code)

    return DealershipRecord(
        dealer_code=dealer_code,
        name=name,
        manager=None,
        province=province,
        city=city,
        address=address,
        phone_numbers=phone_numbers,
        agency_type=agency_type,
        raw_row={
            "cells": [cell if cell is not None else "" for cell in raw_cells],
            "logical_cells": [cell if cell is not None else "" for cell in logical],
            "row_number": row_number,
            "city_code": city_code,
            "sales_phone": sales_phone,
            "service_phone": service_phone,
            "page": page_index,
            "row_index": row_index,
        },
    )


def extract_records_from_pdf(pdf_path: Path) -> list[DealershipRecord]:
    records: list[DealershipRecord] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            table = page.extract_table() or []
            LOGGER.debug("Page %d: %d row(s)", page_index, len(table))

            for row_index, raw_row in enumerate(table):
                if not raw_row or not any(cell for cell in raw_row):
                    continue

                logical_preview = _logical_columns(list(raw_row))
                if _is_title_or_header_row(logical_preview):
                    continue

                record = parse_data_row(
                    list(raw_row),
                    page_index=page_index,
                    row_index=row_index,
                )
                if record is None:
                    LOGGER.debug(
                        "Skipped page=%d row=%d (no valid dealer_code)",
                        page_index,
                        row_index,
                    )
                    continue
                records.append(record)

    return records


def dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
    best: dict[str, DealershipRecord] = {}

    def score(record: DealershipRecord) -> int:
        return sum(
            [
                bool(record.name),
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
        existing = best.get(key)
        if existing is None or score(record) > score(existing):
            best[key] = record

    return list(best.values())


def save_records(records: list[DealershipRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def crawl(pdf_path: Path, output_path: Path) -> int:
    if not pdf_path.is_file():
        print(f"[!] PDF not found: {pdf_path.resolve()}")
        return 1

    records = extract_records_from_pdf(pdf_path)
    records = dedupe_records(records)

    if not records:
        print("[-] No BAMCO dealer records extracted from PDF.")
        return 1

    save_records(records, output_path)
    print(f"[+] Successfully crawled {len(records)} BAMCO dealer(s) from {pdf_path.name}")
    print(f"[+] Saved to {output_path.resolve()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse BAMCO/Bahman dealer PDF list.")
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
