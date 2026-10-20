#!/usr/bin/env python3
"""
Export crawled article/specs JSON(L) files into one multi-sheet Excel workbook
for the product team.

Usage:
  docker exec seenad_backend python3 scripts/export_crawl_to_excel.py
  python scripts/export_crawl_to_excel.py --output data/product_crawl_export.xlsx
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

EXCEL_CELL_LIMIT = 32000
DEFAULT_OUTPUT = Path("data/product_crawl_export.xlsx")

SOURCES = {
    "مقالات_همراه‌مکانیک": Path("data/raw_hamrahmechanic_car_reviews.jsonl"),
    "مقالات_خودرو45": Path("data/raw_khodro45_review_compare.jsonl"),
    "مشخصات_آخرین‌خودرو": Path("data/raw_akharinkhodro_cars.json"),
    "مشخصات_خودروبانک": Path("data/raw_khodrobank_cars.jsonl"),
    "مشخصات_اسب‌بخار": Path("data/raw_asbebokhar_cars.jsonl"),
}


def clip(value: Any, limit: int = EXCEL_CELL_LIMIT) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[truncated]…"


def join_list(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return " | ".join(parts) if parts else None
    return clip(value)


def specs_to_text(specs: Any) -> str | None:
    if not isinstance(specs, dict) or not specs:
        return None
    lines = [f"{k}: {v}" for k, v in specs.items() if k]
    return clip("\n".join(lines))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return load_jsonl(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("articles"), list):
        return payload["articles"]
    if isinstance(payload, list):
        return payload
    raise ValueError(f"Unsupported JSON shape: {path}")


def flatten_article(row: dict[str, Any], *, source_label: str) -> dict[str, Any]:
    return {
        "source_label": source_label,
        "id": row.get("id") or row.get("car_id"),
        "title": clip(row.get("title") or row.get("headline") or row.get("name")),
        "url": row.get("url"),
        "slug": row.get("slug") or row.get("model_slug"),
        "brand": clip(row.get("brand")),
        "brand_slug": row.get("brand_slug"),
        "date": row.get("date") or row.get("date_published"),
        "modified": row.get("modified") or row.get("date_modified"),
        "author": clip(row.get("author")),
        "categories": join_list(row.get("categories") or row.get("term_names")),
        "tags": join_list(row.get("tags")),
        "excerpt": clip(row.get("excerpt") or row.get("description")),
        "featured_image": row.get("featured_image") or row.get("image"),
        "specs": specs_to_text(row.get("specs")),
        "content_text": clip(row.get("content_text")),
        "rating_value": row.get("rating_value"),
        "rating_count": row.get("rating_count"),
        "release_date": row.get("release_date") or row.get("year_label"),
        "crawled_at": row.get("crawled_at"),
        "source": row.get("source"),
    }


def flatten_specs_row(row: dict[str, Any], *, source_label: str) -> dict[str, Any]:
    base = flatten_article(row, source_label=source_label)
    # Prefer listing/model naming for catalogs
    if not base.get("title"):
        base["title"] = clip(row.get("listing_title") or row.get("model"))
    return base


def build_summary(sheet_counts: dict[str, int], generated_at: str) -> pd.DataFrame:
    rows = [
        {
            "sheet": "مقالات_همراه‌مکانیک",
            "type": "مقاله",
            "site": "hamrah-mechanic.com/mag/category/car-reviews/",
            "records": sheet_counts.get("مقالات_همراه‌مکانیک", 0),
            "raw_file": "data/raw_hamrahmechanic_car_reviews.jsonl",
            "notes": "معرفی خودرو — متن کامل در content_text",
        },
        {
            "sheet": "مقالات_خودرو45",
            "type": "مقاله",
            "site": "khodro45.com/mag/category/review-compare/",
            "records": sheet_counts.get("مقالات_خودرو45", 0),
            "raw_file": "data/raw_khodro45_review_compare.jsonl",
            "notes": "بررسی و مقایسه — متن کامل در content_text",
        },
        {
            "sheet": "مشخصات_آخرین‌خودرو",
            "type": "مشخصات فنی",
            "site": "akharinkhodro.ir/category/cars/",
            "records": sheet_counts.get("مشخصات_آخرین‌خودرو", 0),
            "raw_file": "data/raw_akharinkhodro_cars.json",
            "notes": "بانک مشخصات + متن معرفی؛ specs جداگانه",
        },
        {
            "sheet": "مشخصات_خودروبانک",
            "type": "مشخصات فنی",
            "site": "khodrobank.com/cars",
            "records": sheet_counts.get("مشخصات_خودروبانک", 0),
            "raw_file": "data/raw_khodrobank_cars.jsonl",
            "notes": "کاتالوگ برند/مدل؛ specs + امتیاز",
        },
        {
            "sheet": "مشخصات_اسب‌بخار",
            "type": "مشخصات فنی",
            "site": "asbe-bokhar.com/cars/",
            "records": sheet_counts.get("مشخصات_اسب‌بخار", 0),
            "raw_file": "data/raw_asbebokhar_cars.jsonl",
            "notes": "بانک مشخصات؛ specs در ستون specs",
        },
    ]
    frame = pd.DataFrame(rows)
    frame.insert(0, "generated_at_utc", generated_at)
    frame["total_records_all_sheets"] = sum(sheet_counts.values())
    return frame


def export(output: Path) -> Path:
    generated_at = datetime.now(timezone.utc).isoformat()
    sheet_frames: dict[str, pd.DataFrame] = {}
    sheet_counts: dict[str, int] = {}

    article_sheets = {"مقالات_همراه‌مکانیک", "مقالات_خودرو45"}
    for sheet_name, path in SOURCES.items():
        if not path.exists():
            raise FileNotFoundError(f"Missing raw file: {path}")
        records = load_records(path)
        if sheet_name in article_sheets:
            flat = [flatten_article(r, source_label=sheet_name) for r in records]
        else:
            flat = [flatten_specs_row(r, source_label=sheet_name) for r in records]
        frame = pd.DataFrame(flat)
        # Stable column order
        preferred = [
            "source_label",
            "id",
            "title",
            "brand",
            "brand_slug",
            "url",
            "slug",
            "date",
            "modified",
            "author",
            "categories",
            "tags",
            "release_date",
            "rating_value",
            "rating_count",
            "specs",
            "excerpt",
            "content_text",
            "featured_image",
            "crawled_at",
            "source",
        ]
        cols = [c for c in preferred if c in frame.columns] + [
            c for c in frame.columns if c not in preferred
        ]
        frame = frame.reindex(columns=cols)
        sheet_frames[sheet_name] = frame
        sheet_counts[sheet_name] = len(frame)
        print(f"[+] {sheet_name}: {len(frame)} rows from {path}")

    summary = build_summary(sheet_counts, generated_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="خلاصه", index=False)
        for sheet_name, frame in sheet_frames.items():
            # Excel sheet name max 31 chars — our names fit
            frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    print(f"[+] wrote {output.resolve()} ({output.stat().st_size / 1e6:.1f} MB)")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Export crawl raw files to multi-sheet Excel.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    export(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
