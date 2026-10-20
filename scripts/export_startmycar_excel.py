#!/usr/bin/env python3
"""
Export StartMyCar crawl JSONL to a product-team Excel workbook.

Usage:
  python scripts/export_startmycar_excel.py
  python scripts/export_startmycar_excel.py \\
      --input data/raw_startmycar_guides_reviews.jsonl \\
      --output data/startmycar_product_export.xlsx
  docker exec seenad_backend python3 scripts/export_startmycar_excel.py

Dependencies (project requirements.txt):
  pandas, openpyxl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

EXCEL_CELL_LIMIT = 32000
DEFAULT_INPUT = Path("data/raw_startmycar_guides_reviews.jsonl")
DEFAULT_OUTPUT = Path("data/startmycar_product_export.xlsx")

PLANNED_BRANDS = [
    "toyota",
    "ssangyong",
    "volkswagen",
    "mercedes-benz",
    "bmw",
    "mazda",
    "hyundai",
    "kia",
]
PRIORITY_CATEGORIES = frozenset({"lubrication", "maintenance", "fuel"})


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


def yes_no(flag: Any) -> str:
    return "بله" if flag else "خیر"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    bad = 0
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
                print(f"[!] skip bad JSONL line {line_no}")
    if bad:
        print(f"[!] dropped {bad} unreadable line(s)")
    return rows


def join_urls(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    return clip(" | ".join(str(x) for x in value if x))


def flatten_guide(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "برند": row.get("brand"),
        "اسلاگ_برند": row.get("brand_slug"),
        "مدل": row.get("model"),
        "اسلاگ_مدل": row.get("model_slug"),
        "دسته": row.get("category"),
        "اولویت_روغن_سرویس_سوخت": yes_no(row.get("priority")),
        "عنوان": clip(row.get("title") or row.get("listing_title")),
        "اسلاگ_راهنما": row.get("slug"),
        "لینک": row.get("url"),
        "خلاصه": clip(row.get("excerpt"), 1500),
        "متن": clip(row.get("content_text")),
        "تعداد_عکس": len(row.get("images") or []) if isinstance(row.get("images"), list) else 0,
        "عکس‌ها": join_urls(row.get("images")),
        "عکس_لیست": row.get("teaser_image"),
        "زمان_کراول": row.get("crawled_at"),
        "منبع": row.get("source"),
    }


def flatten_review(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "برند": row.get("brand"),
        "اسلاگ_برند": row.get("brand_slug"),
        "مدل": row.get("model"),
        "اسلاگ_مدل": row.get("model_slug"),
        "تریم": clip(row.get("trim")),
        "ستاره": row.get("stars"),
        "بهترین": clip(row.get("best"), 4000),
        "بدترین": clip(row.get("worst"), 4000),
        "نویسنده": clip(row.get("author")),
        "کشور": clip(row.get("author_country")),
        "تاریخ": row.get("posted_at") or row.get("posted_label"),
        "رأی": row.get("votes"),
        "میانگین_مدل": row.get("average_score"),
        "تعداد_اعلامی_سایت": row.get("review_count_claimed"),
        "لینک_صفحه": row.get("url"),
        "شناسه": row.get("id"),
        "زمان_کراول": row.get("crawled_at"),
        "منبع": row.get("source"),
    }


def build_coverage(records: list[dict[str, Any]]) -> pd.DataFrame:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        brand_slug = row.get("brand_slug") or ""
        model_slug = row.get("model_slug") or ""
        key = (brand_slug, model_slug)
        item = buckets.setdefault(
            key,
            {
                "برند": row.get("brand"),
                "اسلاگ_برند": brand_slug,
                "مدل": row.get("model"),
                "اسلاگ_مدل": model_slug,
                "تعداد_راهنما": 0,
                "راهنمای_اولویت": 0,
                "تعداد_ریویو": 0,
                "میانگین_امتیاز": None,
                "تعداد_اعلامی_ریویو": None,
            },
        )
        if row.get("record_type") == "guide":
            item["تعداد_راهنما"] += 1
            if row.get("priority"):
                item["راهنمای_اولویت"] += 1
        elif row.get("record_type") == "review":
            item["تعداد_ریویو"] += 1
            item["میانگین_امتیاز"] = row.get("average_score")
            item["تعداد_اعلامی_ریویو"] = row.get("review_count_claimed")
    frame = pd.DataFrame(list(buckets.values()))
    if frame.empty:
        return frame
    return frame.sort_values(["اسلاگ_برند", "اسلاگ_مدل"], kind="stable").reset_index(drop=True)


def build_summary(
    *,
    generated_at: str,
    input_path: Path,
    records: list[dict[str, Any]],
    coverage: pd.DataFrame,
) -> pd.DataFrame:
    types = Counter(r.get("record_type") for r in records)
    brand_models = Counter()
    brand_guides = Counter()
    brand_reviews = Counter()
    for row in records:
        brand = row.get("brand_slug") or ""
        if row.get("record_type") == "guide":
            brand_guides[brand] += 1
        elif row.get("record_type") == "review":
            brand_reviews[brand] += 1
    if not coverage.empty:
        brand_models = Counter(coverage["اسلاگ_برند"].tolist())

    rows: list[dict[str, Any]] = [
        {
            "بخش": "وضعیت کراول",
            "مقدار": "متوقف‌شده قبل از اتمام (درخواست تیم)",
            "توضیح": "BMW / Mazda / Hyundai / Kia شروع نشده؛ Mercedes-Benz ناقص",
        },
        {
            "بخش": "منبع",
            "مقدار": "startmycar.com",
            "توضیح": "guides + reviews — بدون ترجمه",
        },
        {
            "بخش": "فایل خام",
            "مقدار": str(input_path),
            "توضیح": generated_at,
        },
        {
            "بخش": "کل رکورد",
            "مقدار": len(records),
            "توضیح": f"راهنما={types.get('guide', 0)} | ریویو={types.get('review', 0)}",
        },
        {
            "بخش": "تعداد مدل",
            "مقدار": 0 if coverage.empty else len(coverage),
            "توضیح": "مدل‌هایی که حداقل یک رکورد دارند",
        },
    ]
    for slug in PLANNED_BRANDS:
        rows.append(
            {
                "بخش": f"برند {slug}",
                "مقدار": f"مدل={brand_models.get(slug, 0)} | راهنما={brand_guides.get(slug, 0)} | ریویو={brand_reviews.get(slug, 0)}",
                "توضیح": "کامل" if brand_models.get(slug, 0) and slug not in {"mercedes-benz", "bmw", "mazda", "hyundai", "kia"} else (
                    "شروع نشده" if brand_models.get(slug, 0) == 0 else "ناقص — کراول وسط کار قطع شد"
                ),
            }
        )
    rows.append(
        {
            "بخش": "اولویت سلمان",
            "مقدار": sum(1 for r in records if r.get("record_type") == "guide" and r.get("priority")),
            "توضیح": "Lubrication + Maintenance + Fuel",
        }
    )
    rows.append(
        {
            "بخش": "محدودیت ریویو",
            "مقدار": "حداکثر حدود ۱۰۰ کارت در HTML",
            "توضیح": "اگر سایت عدد بیشتری اعلام کند، بقیه در صفحه نیست",
        }
    )
    return pd.DataFrame(rows)


def export(input_path: Path, output_path: Path) -> Path:
    generated_at = datetime.now(timezone.utc).isoformat()
    records = load_jsonl(input_path)
    if not records:
        raise SystemExit(f"Empty input: {input_path}")

    guides = [flatten_guide(r) for r in records if r.get("record_type") == "guide"]
    priority = [
        flatten_guide(r)
        for r in records
        if r.get("record_type") == "guide" and r.get("priority")
    ]
    reviews = [flatten_review(r) for r in records if r.get("record_type") == "review"]
    coverage = build_coverage(records)
    summary = build_summary(
        generated_at=generated_at,
        input_path=input_path,
        records=records,
        coverage=coverage,
    )

    guides_df = pd.DataFrame(guides)
    priority_df = pd.DataFrame(priority)
    reviews_df = pd.DataFrame(reviews)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="خلاصه", index=False)
        coverage.to_excel(writer, sheet_name="پوشش_مدل‌ها", index=False)
        priority_df.to_excel(writer, sheet_name="راهنماهای_اولویت", index=False)
        guides_df.to_excel(writer, sheet_name="همه_راهنماها", index=False)
        reviews_df.to_excel(writer, sheet_name="ریویوها", index=False)

    print(f"[+] guides={len(guides_df)} priority={len(priority_df)} reviews={len(reviews_df)} models={len(coverage)}")
    print(f"[+] wrote {output_path.resolve()} ({output_path.stat().st_size / 1e6:.1f} MB)")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export StartMyCar JSONL to product Excel.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.input.exists():
        raise SystemExit(f"Missing input: {args.input}")
    export(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
