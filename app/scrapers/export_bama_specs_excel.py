#!/usr/bin/env python3
"""
Export ``bama_scraped_specs.jsonl`` to a product-manager-friendly Excel workbook.

Sheets:
  - خلاصه خودروها: one row per scraped car (metadata + counts)
  - مشخصات تفصیلی: long format (car × section × feature × value)
  - خطاهای استخراج: failed URLs from scraper progress (if present)

Usage::

    python app/scrapers/export_bama_specs_excel.py
    python app/scrapers/export_bama_specs_excel.py --input bama_scraped_specs.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from openpyxl.utils import get_column_letter

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSONL = PROJECT_ROOT / "bama_scraped_specs.jsonl"
DEFAULT_PROGRESS = Path(__file__).resolve().parent / "scraper_progress.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "bama_scraped_specs.xlsx"
MAX_COLUMN_WIDTH = 72


def _format_value(value: Any) -> str:
    if value is True:
        return "بله"
    if value is False:
        return "خیر"
    if value is None:
        return ""
    return str(value)


def _parse_car_identity(url: str) -> tuple[str, str, str]:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    brand = parts[1] if len(parts) > 1 else ""
    model_slug = parts[2] if len(parts) > 2 else ""
    display = model_slug.replace("-specs-", " / ").replace("-", " ")
    return brand, model_slug, display


def load_jsonl_records(jsonl_path: Path) -> list[dict[str, Any]]:
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"JSONL file not found: {jsonl_path}")

    records: list[dict[str, Any]] = []
    with jsonl_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                LOGGER.warning("Skipping invalid JSONL line %d: %s", line_no, exc)
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def build_summary_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        url = str(record.get("url", ""))
        brand, model_slug, model_display = _parse_car_identity(url)
        rows.append(
            {
                "لینک صفحه": url,
                "برند": brand,
                "مدل (اسلاگ)": model_slug,
                "مدل (خوانا)": model_display,
                "تاریخ استخراج": str(record.get("scraped_at", "")),
                "تعداد بخش‌ها": str(record.get("section_count", "")),
                "تعداد ویژگی‌ها": str(record.get("item_count", "")),
            }
        )
    return rows


def build_detail_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        url = str(record.get("url", ""))
        brand, model_slug, model_display = _parse_car_identity(url)
        scraped_at = str(record.get("scraped_at", ""))
        specs = record.get("specs")
        if not isinstance(specs, dict):
            continue

        for section_title, items in specs.items():
            if not isinstance(items, dict):
                continue
            for feature, value in items.items():
                rows.append(
                    {
                        "لینک صفحه": url,
                        "برند": brand,
                        "مدل (اسلاگ)": model_slug,
                        "مدل (خوانا)": model_display,
                        "تاریخ استخراج": scraped_at,
                        "بخش": str(section_title),
                        "ویژگی": str(feature),
                        "مقدار": _format_value(value),
                    }
                )
    return rows


def build_failure_rows(progress_path: Path) -> list[dict[str, str]]:
    if not progress_path.is_file():
        return []

    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Could not parse progress file: %s", progress_path)
        return []

    processed = progress.get("processed", {})
    if not isinstance(processed, dict):
        return []

    rows: list[dict[str, str]] = []
    for url, entry in processed.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "FAILED":
            continue
        brand, model_slug, model_display = _parse_car_identity(str(url))
        rows.append(
            {
                "لینک صفحه": str(url),
                "برند": brand,
                "مدل (اسلاگ)": model_slug,
                "مدل (خوانا)": model_display,
                "تاریخ آخرین تلاش": str(entry.get("scraped_at", "")),
                "پیام خطا": str(entry.get("error") or ""),
            }
        )
    return rows


def autofit_columns(worksheet: Any, *, max_width: int = MAX_COLUMN_WIDTH) -> None:
    for column_index, column_cells in enumerate(
        worksheet.iter_cols(1, worksheet.max_column),
        start=1,
    ):
        max_length = 0
        for cell in column_cells:
            if cell.value is None:
                continue
            max_length = max(max_length, len(str(cell.value)))
        width = min(max(max_length + 2, 12), max_width)
        worksheet.column_dimensions[get_column_letter(column_index)].width = width


def export_to_excel(
    jsonl_path: Path,
    output_path: Path,
    progress_path: Path,
) -> Path:
    records = load_jsonl_records(jsonl_path)
    if not records:
        raise ValueError(f"No records found in {jsonl_path}")

    summary_df = pd.DataFrame(build_summary_rows(records))
    detail_df = pd.DataFrame(build_detail_rows(records))
    failures_df = pd.DataFrame(build_failure_rows(progress_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="خلاصه خودروها", index=False)
        detail_df.to_excel(writer, sheet_name="مشخصات تفصیلی", index=False)
        if not failures_df.empty:
            failures_df.to_excel(writer, sheet_name="خطاهای استخراج", index=False)

        for sheet_name in writer.sheets:
            autofit_columns(writer.sheets[sheet_name])

    LOGGER.info(
        "Exported %s → %s (cars=%d, detail_rows=%d, failures=%d)",
        jsonl_path.name,
        output_path.name,
        len(summary_df),
        len(detail_df),
        len(failures_df),
    )
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Bama scraped specs JSONL to Excel.")
    parser.add_argument("--input", type=Path, default=DEFAULT_JSONL, help="Source JSONL file")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output .xlsx path")
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=DEFAULT_PROGRESS,
        help="Scraper progress JSON for failure sheet",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s | %(message)s",
    )

    try:
        output = export_to_excel(args.input, args.output, args.progress_file)
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1

    print(f"Excel آماده شد: {output.resolve()}")
    print(f"تعداد خودرو: {len(load_jsonl_records(args.input))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
