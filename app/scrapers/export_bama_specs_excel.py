#!/usr/bin/env python3
"""
Export Bama car specs (nested JSON or JSONL) to a product-manager Excel workbook.

Sheets:
  - خلاصه تریم‌ها: one row per trim (identity + scores + key specs)
  - مشخصات تفصیلی: long format (trim × section × feature × value)
  - امتیاز و نظرات: scores, strengths, weaknesses
  - خطاهای استخراج: failed URLs from scraper progress (if present)

Usage::

    python app/scrapers/export_bama_specs_excel.py
    python app/scrapers/export_bama_specs_excel.py --input data/raw_bama_car_specs.json
    docker exec -i seenad_backend python3 app/scrapers/export_bama_specs_excel.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON = PROJECT_ROOT / "data" / "raw_bama_car_specs.json"
DEFAULT_PROGRESS = PROJECT_ROOT / "data" / "bama_specs_progress.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw_bama_car_specs.xlsx"
MAX_COLUMN_WIDTH = 56

KEY_SPEC_COLUMNS: list[tuple[str, str, str]] = [
    ("وضعیت در بازار ایران", "سال های موجود", "سال‌های موجود"),
    ("مشخصات فنی", "محور محرک", "محور محرک"),
    ("مشخصات فنی", "گیربکس", "گیربکس"),
    ("مشخصات فنی", "پیشرانه", "پیشرانه"),
    ("مشخصات فنی", "حجم موتور", "حجم موتور"),
    ("عملکرد فنی", "قدرت", "قدرت"),
    ("عملکرد فنی", "گشتاور", "گشتاور"),
    ("عملکرد فنی", "شتاب", "شتاب"),
    ("عملکرد فنی", "سرعت", "سرعت"),
    ("عملکرد فنی", "مصرف ترکیبی", "مصرف ترکیبی"),
    ("ابعاد و اندازه ها", "نوع بدنه", "نوع بدنه"),
]


def _format_value(value: Any) -> str:
    if value is True:
        return "بله"
    if value is False:
        return "خیر"
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(_format_value(item) for item in value if _format_value(item))
    return str(value)


def _spec_value(specs: Any, section: str, key: str) -> str:
    if not isinstance(specs, dict):
        return ""
    items = specs.get(section)
    if not isinstance(items, dict):
        return ""
    return _format_value(items.get(key))


def _parse_car_identity(url: str) -> tuple[str, str, str]:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    brand = parts[1] if len(parts) > 1 else ""
    model_slug = parts[2] if len(parts) > 2 else ""
    display = model_slug.replace("-specs-", " / ").replace("-", " ")
    return brand, model_slug, display


def load_nested_models(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError(f"Expected nested specs JSON with models[]: {path}")
    return payload["models"], str(payload.get("crawled_at") or "")


def flatten_trims(models: list[dict[str, Any]], crawled_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        trims = model.get("trims")
        if not isinstance(trims, list):
            continue
        for trim in trims:
            if not isinstance(trim, dict):
                continue
            rows.append(
                {
                    "brand_slug": str(model.get("brand_slug") or ""),
                    "model_slug": str(model.get("model_slug") or ""),
                    "model_id": str(model.get("model_id") or ""),
                    "title": str(model.get("title") or ""),
                    "trim_slug": str(trim.get("trim_slug") or ""),
                    "trim_name": str(trim.get("trim_name") or ""),
                    "url": str(trim.get("url") or ""),
                    "year_label": str(trim.get("year_label") or ""),
                    "review": trim.get("review") if isinstance(trim.get("review"), dict) else {},
                    "section_count": trim.get("section_count", ""),
                    "item_count": trim.get("item_count", ""),
                    "specs": trim.get("specs") if isinstance(trim.get("specs"), dict) else {},
                    "scraped_at": str(trim.get("scraped_at") or crawled_at),
                    "model_trim_count": model.get("trim_count", len(trims)),
                }
            )
    return rows


def build_summary_rows(trims: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for trim in trims:
        review = trim["review"]
        scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
        row: dict[str, str] = {
            "برند": trim["brand_slug"],
            "مدل": trim["title"] or trim["model_slug"],
            "شناسه مدل": trim["model_id"],
            "تریم": trim["trim_name"] or trim["trim_slug"] or "(بدون تریم)",
            "اسلاگ تریم": trim["trim_slug"],
            "سال": trim["year_label"],
            "تعداد تریم مدل": str(trim["model_trim_count"]),
            "تعداد بخش‌ها": str(trim["section_count"]),
            "تعداد ویژگی‌ها": str(trim["item_count"]),
            "امتیاز نهایی": _format_value(review.get("final_score")),
            "رانندگی و فنی": _format_value(scores.get("رانندگی و فنی")),
            "طراحی ظاهری": _format_value(scores.get("طراحی ظاهری")),
            "کابین و امکانات": _format_value(scores.get("کابین و امکانات")),
            "ارزش در مقابل قیمت": _format_value(scores.get("ارزش در مقابل قیمت")),
            "کیفیت": _format_value(scores.get("کیفیت")),
            "لینک صفحه": trim["url"],
            "تاریخ استخراج": trim["scraped_at"],
        }
        for section, key, column in KEY_SPEC_COLUMNS:
            row[column] = _spec_value(trim["specs"], section, key)
        rows.append(row)
    return rows


def build_detail_rows(trims: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for trim in trims:
        for section_title, items in trim["specs"].items():
            if not isinstance(items, dict):
                continue
            for feature, value in items.items():
                rows.append(
                    {
                        "برند": trim["brand_slug"],
                        "مدل": trim["title"] or trim["model_slug"],
                        "شناسه مدل": trim["model_id"],
                        "تریم": trim["trim_name"] or trim["trim_slug"] or "(بدون تریم)",
                        "سال": trim["year_label"],
                        "بخش": str(section_title),
                        "ویژگی": str(feature),
                        "مقدار": _format_value(value),
                        "لینک صفحه": trim["url"],
                    }
                )
    return rows


def build_review_rows(trims: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for trim in trims:
        review = trim["review"]
        scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
        rows.append(
            {
                "برند": trim["brand_slug"],
                "مدل": trim["title"] or trim["model_slug"],
                "تریم": trim["trim_name"] or trim["trim_slug"] or "(بدون تریم)",
                "امتیاز نهایی": _format_value(review.get("final_score")),
                "رانندگی و فنی": _format_value(scores.get("رانندگی و فنی")),
                "طراحی ظاهری": _format_value(scores.get("طراحی ظاهری")),
                "کابین و امکانات": _format_value(scores.get("کابین و امکانات")),
                "ارزش در مقابل قیمت": _format_value(scores.get("ارزش در مقابل قیمت")),
                "کیفیت": _format_value(scores.get("کیفیت")),
                "نقاط قوت": _format_value(review.get("strengths")),
                "نقاط ضعف": _format_value(review.get("weaknesses")),
                "لینک صفحه": trim["url"],
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
        if not isinstance(entry, dict) or entry.get("status") != "FAILED":
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


def _add_table(worksheet: Any, name: str) -> None:
    if worksheet.max_row < 2 or worksheet.max_column < 1:
        return
    last_col = get_column_letter(worksheet.max_column)
    ref = f"A1:{last_col}{worksheet.max_row}"
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    worksheet.add_table(table)


def export_to_excel(
    json_path: Path,
    output_path: Path,
    progress_path: Path,
) -> Path:
    models, crawled_at = load_nested_models(json_path)
    trims = flatten_trims(models, crawled_at)
    if not trims:
        raise ValueError(f"No trim records found in {json_path}")

    summary_df = pd.DataFrame(build_summary_rows(trims))
    detail_df = pd.DataFrame(build_detail_rows(trims))
    review_df = pd.DataFrame(build_review_rows(trims))
    failures_df = pd.DataFrame(build_failure_rows(progress_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="خلاصه تریم‌ها", index=False)
        detail_df.to_excel(writer, sheet_name="مشخصات تفصیلی", index=False)
        review_df.to_excel(writer, sheet_name="امتیاز و نظرات", index=False)
        if not failures_df.empty:
            failures_df.to_excel(writer, sheet_name="خطاهای استخراج", index=False)

        table_names = {
            "خلاصه تریم‌ها": "Trims",
            "مشخصات تفصیلی": "Specs",
            "امتیاز و نظرات": "Reviews",
            "خطاهای استخراج": "Failures",
        }
        for sheet_name, worksheet in writer.sheets.items():
            autofit_columns(worksheet)
            worksheet.freeze_panes = "A2"
            _add_table(worksheet, table_names.get(sheet_name, sheet_name))

    LOGGER.info(
        "Exported %s → %s (models=%d, trims=%d, detail_rows=%d, failures=%d)",
        json_path.name,
        output_path.name,
        len(models),
        len(summary_df),
        len(detail_df),
        len(failures_df),
    )
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Bama nested car specs JSON to Excel.")
    parser.add_argument("--input", type=Path, default=DEFAULT_JSON, help="Source nested JSON file")
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
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("%s", exc)
        return 1

    models, _ = load_nested_models(args.input)
    trim_count = sum(len(m.get("trims") or []) for m in models)
    print(f"Excel آماده شد: {output.resolve()}")
    print(f"مدل: {len(models)} | تریم: {trim_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
