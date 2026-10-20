"""Table-first vehicle extraction with header synonym mapping + prose fallback."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from src.scraper.sell_news.constants import EXTRACT_HTML_TABLE, EXTRACT_KEY_VALUE, EXTRACT_PROSE
from src.scraper.sell_news.normalize import (
    BRAND_PREFIXES,
    clean_text,
    html_to_text,
    is_four_digit_year,
    parse_price,
    split_brand_model,
)
from src.scraper.sell_news.records import CarRow

# Canonical fields mapped from noisy Persian headers.
FIELD_VEHICLE = "vehicle"
FIELD_BRAND = "brand"
FIELD_MODEL = "model"
FIELD_TRIM = "trim"
FIELD_COLOR = "color"
FIELD_PRICE = "price"
FIELD_PLAN = "plan"
FIELD_DELIVERY = "delivery"
FIELD_COMPANY = "company"
FIELD_PREPAY = "prepay"
FIELD_PENALTY = "penalty"
FIELD_OTHER = "other"

HEADER_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    (
        FIELD_VEHICLE,
        (
            "نام خودرو",
            "نوع خودرو",
            "نام محصول",
            "توضیحات/مدل خودرو",
            "خودرو",
        ),
    ),
    (FIELD_BRAND, ("برند", "شرکت سازنده")),
    (FIELD_MODEL, ("مدل خودرو", "سال مدل", "مدل")),
    (FIELD_TRIM, ("تریم", "تیپ", "گونه")),
    (FIELD_COLOR, ("رنگ قابل عرضه", "رنگ انتخابی", "رنگ")),
    (
        FIELD_PRICE,
        (
            "قیمت قطعی",
            "مبلغ نهایی خودرو",
            "مبلغ نهایی",
            "قیمت نهایی برای مصرف کننده",
            "قیمت نهایی در مبادی ورودی",
            "قیمت نهایی",
            "قیمت مصرف کننده",
            "قیمت خودرو",
            "قیمت مصوب",
            "قیمت (تومان)",
            "قیمت (ریال)",
            "قیمت جدید",
            "قیمت قبلی",
            "قیمت علی الحساب",
            "قیمت",
        ),
    ),
    (FIELD_PLAN, ("نام طرح", "نوع طرح", "نوع فروش", "روش فروش", "طرح")),
    (FIELD_DELIVERY, ("زمان تحویل", "موعد تحویل", "تحویل")),
    (FIELD_COMPANY, ("شرکت واردکننده", "شرکت", "واردکننده")),
    (FIELD_PREPAY, ("پیش پرداخت", "پیش‌پرداخت", "مبلغ پیش پرداخت")),
    (FIELD_PENALTY, ("جریمه تاخیر", "جریمه تأخیر", "سود تاخیر", "تاخیر در تحویل")),
]

# Longer phrases first so "مدل خودرو" is not eaten as "مدل" when we want vehicle.
_HEADER_LOOKUP: list[tuple[str, str]] = []
for _field, _names in HEADER_SYNONYMS:
    for _name in _names:
        _HEADER_LOOKUP.append((_name, _field))
_HEADER_LOOKUP.sort(key=lambda x: len(x[0]), reverse=True)

CAR_SIGNAL_FIELDS = {
    FIELD_VEHICLE,
    FIELD_BRAND,
    FIELD_MODEL,
    FIELD_PRICE,
    FIELD_DELIVERY,
    FIELD_COLOR,
    FIELD_PLAN,
}

SKIP_TABLE_HINTS = (
    "اسامی سازمان",
    "سازمان های مجاز",
    "سازمان‌های مجاز",
    "مرحله اجرایی",
    "تاریخ شروع بازه",
    "پیشنهاد سردبیر",
    "نظرات کاربران",
)

PROSE_PRICE_RE = re.compile(
    r"(?:قیمت(?:\s*قطعی|\s*نهایی)?|مبلغ(?:\s*نهایی)?)\s*[:\-]?\s*"
    r"([\d۰-۹0-9,\.،]+(?:\s*(?:میلیارد|میلیون))?(?:\s*و\s*[\d۰-۹0-9,\.،]+\s*میلیون)?\s*(?:تومان|ریال|ريال)?)",
    re.I,
)
PROSE_DELIVERY_RE = re.compile(
    r"(?:زمان\s*تحویل|موعد\s*تحویل|تحویل)\s*[:\-]?\s*([^\n.]{2,60})"
)
PROSE_PLAN_RE = re.compile(
    r"(فروش\s*فوری|پیش[-\u200c\s]*فروش|فروش\s*فوق[-\u200c\s]*العاده|فروش\s*ویژه|"
    r"فروش\s*سازمانی|قرعه[-\u200c\s]*کشی)"
)


def map_header(cell: str | None) -> str | None:
    text = _norm_header(cell)
    if not text:
        return None
    for name, field in _HEADER_LOOKUP:
        n = _norm_header(name)
        if not n:
            continue
        if text == n or text.startswith(n + " ") or text.startswith(n + "("):
            return field
    return None


def _norm_header(value: str | None) -> str:
    text = clean_text(value) or ""
    text = text.replace("‌", " ").replace(")", " ").replace("(", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def repair_broken_table_html(html: str) -> str:
    """Khodrobank often inserts <p> between table tags; strip those wrappers."""

    def _clean_match(match: re.Match[str]) -> str:
        raw = match.group(0)
        return re.sub(r"</?p\b[^>]*>", "", raw, flags=re.I)

    return re.sub(r"<table\b[^>]*>[\s\S]*?</table>", _clean_match, html, flags=re.I)


def iter_tables(html: str | None) -> list[Tag]:
    if not html:
        return []
    repaired = repair_broken_table_html(html)
    soup = BeautifulSoup(repaired, "html.parser")
    return [t for t in soup.find_all("table") if isinstance(t, Tag)]


def html_table_to_matrix(table: Tag) -> list[list[str]]:
    """Expand rowspan/colspan into a rectangular list of lists."""

    pending: dict[int, tuple[int, str]] = {}
    grid: list[list[str]] = []
    for tr in table.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        cells = [c for c in tr.find_all(["th", "td"], recursive=False) if isinstance(c, Tag)]
        row: list[str] = []
        col = 0
        cell_i = 0
        while True:
            if pending.get(col, (0, ""))[0] > 0:
                left, text = pending[col]
                row.append(text)
                pending[col] = (left - 1, text)
                col += 1
                continue
            if cell_i >= len(cells):
                break
            cell = cells[cell_i]
            cell_i += 1
            text = clean_text(cell.get_text(" ", strip=True)) or ""
            try:
                rowspan = max(int(cell.get("rowspan") or 1), 1)
            except (TypeError, ValueError):
                rowspan = 1
            try:
                colspan = max(int(cell.get("colspan") or 1), 1)
            except (TypeError, ValueError):
                colspan = 1
            for _ in range(colspan):
                while pending.get(col, (0, ""))[0] > 0:
                    left, ptext = pending[col]
                    row.append(ptext)
                    pending[col] = (left - 1, ptext)
                    col += 1
                row.append(text)
                if rowspan > 1:
                    pending[col] = (rowspan - 1, text)
                col += 1
        if row:
            grid.append(row)
    return grid


def car_row_key(row: CarRow) -> tuple[str, str, str, str]:
    return (
        (row.brand_raw or "").lower(),
        (row.model_raw or "").lower(),
        row.price_raw or "",
        row.delivery_raw or "",
    )


def extract_cars_from_table(table: Tag, *, title: str | None = None) -> tuple[list[CarRow], str]:
    """Parse one HTML table. Returns (rows, extract_method) — empty if not a sell table."""

    matrix = html_table_to_matrix(table)
    if len(matrix) < 2:
        return [], ""
    if _should_skip_table(matrix):
        return [], ""
    parsed, used = _parse_matrix(matrix, title=title)
    rows = [row for row in parsed if row and not row.is_empty()]
    return rows, (used if rows else "")


def extract_cars_from_html(
    html: str | None,
    *,
    title: str | None = None,
) -> tuple[list[CarRow], str]:
    """Return vehicle rows and the winning extract_method (or empty method)."""

    rows: list[CarRow] = []
    method = EXTRACT_HTML_TABLE
    for table in iter_tables(html):
        parsed, used = extract_cars_from_table(table, title=title)
        if parsed:
            rows.extend(parsed)
            if used == EXTRACT_KEY_VALUE and method == EXTRACT_HTML_TABLE and len(rows) == len(parsed):
                method = EXTRACT_KEY_VALUE
    rows = _dedupe_rows(rows)
    for i, row in enumerate(rows):
        row.row_index = i
        row.mark_incomplete()
    if rows:
        return rows, method
    prose = extract_cars_from_prose(html_to_text(html), title=title)
    return prose, EXTRACT_PROSE if prose else ""


def extract_cars_from_prose(text: str | None, *, title: str | None = None) -> list[CarRow]:
    blob = " ".join(x for x in (title, text) if x)
    blob = clean_text(blob) or ""
    if not blob:
        return []
    price_raw = None
    m = PROSE_PRICE_RE.search(blob)
    if m:
        price_raw = clean_text(m.group(1))
    delivery_raw = None
    m = PROSE_DELIVERY_RE.search(blob)
    if m:
        delivery_raw = clean_text(m.group(1))
        if delivery_raw and len(delivery_raw) > 80:
            delivery_raw = delivery_raw[:80]
    plan_raw = None
    m = PROSE_PLAN_RE.search(blob)
    if m:
        plan_raw = clean_text(m.group(1))
    if not any((price_raw, delivery_raw)):
        return []
    brand, model = split_brand_model(_vehicle_from_title(title) or "")
    toman, unit = parse_price(price_raw)
    row = CarRow(
        row_index=0,
        brand_raw=brand,
        model_raw=model,
        price_raw=price_raw,
        price_toman=toman,
        price_unit_guess=unit,
        plan_name_raw=plan_raw,
        delivery_raw=delivery_raw,
        extract_method=EXTRACT_PROSE,
        extra_json={"source": "title_and_lead"},
    )
    row.mark_incomplete()
    if row.is_empty():
        return []
    return [row]


def _vehicle_from_title(title: str | None) -> str | None:
    text = clean_text(title) or ""
    if not text:
        return None
    lowered = text.lower()
    found_at = None
    found_brand = None
    for brand in BRAND_PREFIXES:
        idx = lowered.find(brand.lower())
        if idx >= 0 and (found_at is None or idx < found_at):
            found_at = idx
            found_brand = brand
    if found_at is not None and found_brand is not None:
        rest = text[found_at:]
        rest = re.split(r"[|\-–—]|مرداد|شهریور|جدول|با تحویل|با قیمت", rest)[0]
        return clean_text(rest)
    text = re.sub(
        r"^(آغاز|اعلام|شرایط|فروش|ثبت\s*نام|فرصت)\s+",
        "",
        text,
    )
    text = re.split(r"[|\-–—]|مرداد|شهریور|جدول", text)[0]
    return clean_text(text)


def _should_skip_table(matrix: list[list[str]]) -> bool:
    joined = " ".join(cell for row in matrix[:3] for cell in row)
    return any(hint in joined for hint in SKIP_TABLE_HINTS)


def _parse_matrix(matrix: list[list[str]], *, title: str | None) -> tuple[list[CarRow], str]:
    if _looks_like_key_value(matrix):
        row = _parse_key_value(matrix, title=title)
        return ([row] if row and not row.is_empty() else []), EXTRACT_KEY_VALUE
    if _looks_like_transposed(matrix):
        rows = _parse_transposed(matrix, title=title)
        return rows, EXTRACT_KEY_VALUE
    rows = _parse_header_table(matrix, title=title)
    return rows, EXTRACT_HTML_TABLE


def _looks_like_key_value(matrix: list[list[str]]) -> bool:
    if len(matrix) < 2:
        return False
    two_col = sum(1 for row in matrix if 1 <= len([c for c in row if c]) <= 2)
    if two_col < max(2, int(0.7 * len(matrix))):
        return False
    mapped = 0
    for row in matrix:
        key = row[0] if row else ""
        if map_header(key) in {
            FIELD_VEHICLE,
            FIELD_BRAND,
            FIELD_MODEL,
            FIELD_PRICE,
            FIELD_DELIVERY,
            FIELD_COLOR,
            FIELD_TRIM,
            FIELD_PLAN,
            FIELD_PREPAY,
            FIELD_PENALTY,
        }:
            mapped += 1
    return mapped >= 2


def _looks_like_transposed(matrix: list[list[str]]) -> bool:
    if not matrix or max(len(r) for r in matrix) < 3:
        return False
    mapped = 0
    for row in matrix:
        if map_header(row[0] if row else None):
            mapped += 1
    return mapped >= 2 and mapped >= len(matrix) // 2


def _parse_key_value(matrix: list[list[str]], *, title: str | None) -> CarRow | None:
    fields: dict[str, str] = {}
    extras: dict[str, Any] = {}
    price_header = None
    for row in matrix:
        if not row:
            continue
        key = row[0]
        val = row[1] if len(row) > 1 else ""
        field = map_header(key)
        if not field:
            if key and val:
                extras[key] = val
            continue
        if field == FIELD_PRICE:
            price_header = key
            fields[field] = val
        elif field == FIELD_OTHER:
            extras[key] = val
        elif field in (FIELD_PREPAY, FIELD_PENALTY, FIELD_COMPANY):
            extras[key] = val
        else:
            fields[field] = val
            if field == FIELD_MODEL:
                year = is_four_digit_year(val)
                if year:
                    extras["model_year"] = year
    return _row_from_fields(
        fields,
        extras=extras,
        price_header=price_header,
        title=title,
        method=EXTRACT_KEY_VALUE,
    )


def _parse_transposed(matrix: list[list[str]], *, title: str | None) -> list[CarRow]:
    if not matrix:
        return []
    width = max(len(r) for r in matrix)
    cars: list[CarRow] = []
    for col in range(1, width):
        fields: dict[str, str] = {}
        extras: dict[str, Any] = {}
        price_header = None
        for row in matrix:
            if not row:
                continue
            key = row[0]
            val = row[col] if col < len(row) else ""
            field = map_header(key)
            if not field or not val:
                continue
            if field == FIELD_PRICE:
                price_header = key
            if field in (FIELD_PREPAY, FIELD_PENALTY, FIELD_COMPANY):
                extras[key] = val
            else:
                fields[field] = val
            if field == FIELD_MODEL:
                year = is_four_digit_year(val)
                if year:
                    extras["model_year"] = year
        row_obj = _row_from_fields(
            fields,
            extras=extras,
            price_header=price_header,
            title=title,
            method=EXTRACT_KEY_VALUE,
        )
        if row_obj and not row_obj.is_empty():
            cars.append(row_obj)
    return cars


def _parse_header_table(matrix: list[list[str]], *, title: str | None) -> list[CarRow]:
    header_idx = _find_header_row(matrix)
    if header_idx is None:
        return []
    header = matrix[header_idx]
    mapping: list[str | None] = [map_header(cell) for cell in header]
    if not any(f in CAR_SIGNAL_FIELDS for f in mapping if f):
        return []
    price_col = _preferred_price_column(header, mapping)
    cars: list[CarRow] = []
    for raw_row in matrix[header_idx + 1 :]:
        if not any(raw_row):
            continue
        fields: dict[str, str] = {}
        extras: dict[str, Any] = {}
        price_header = None
        for i, cell in enumerate(raw_row):
            field = mapping[i] if i < len(mapping) else None
            header_name = header[i] if i < len(header) else ""
            if not cell:
                continue
            if field == FIELD_PRICE:
                if price_col is not None and i != price_col:
                    extras[header_name] = cell
                    continue
                fields[field] = cell
                price_header = header_name
            elif field in {
                FIELD_VEHICLE,
                FIELD_BRAND,
                FIELD_MODEL,
                FIELD_TRIM,
                FIELD_COLOR,
                FIELD_PLAN,
                FIELD_DELIVERY,
            }:
                fields[field] = cell
                if field == FIELD_MODEL:
                    year = is_four_digit_year(cell)
                    if year:
                        extras["model_year"] = year
            elif header_name:
                extras[header_name] = cell
        row_obj = _row_from_fields(
            fields,
            extras=extras,
            price_header=price_header,
            title=title,
            method=EXTRACT_HTML_TABLE,
        )
        if row_obj and not row_obj.is_empty():
            cars.append(row_obj)
    return cars


def _preferred_price_column(header: list[str], mapping: list[str | None]) -> int | None:
    price_cols = [i for i, field in enumerate(mapping) if field == FIELD_PRICE]
    if not price_cols:
        return None

    def score(i: int) -> int:
        name = header[i] if i < len(header) else ""
        s = 0
        if any(w in name for w in ("قطعی", "نهایی", "مصوب", "جدید", "مصرف")):
            s += 5
        if "قبلی" in name or "افزایش" in name:
            s -= 5
        if "علی الحساب" in name or "علی‌الحساب" in name:
            s += 1
        return s

    return max(price_cols, key=score)


def _find_header_row(matrix: list[list[str]]) -> int | None:
    best_i = None
    best_score = 0
    for i, row in enumerate(matrix[:4]):
        score = sum(1 for cell in row if map_header(cell) in CAR_SIGNAL_FIELDS)
        if score > best_score:
            best_score = score
            best_i = i
    return best_i if best_score >= 1 else None


def _row_from_fields(
    fields: dict[str, str],
    *,
    extras: dict[str, Any],
    price_header: str | None,
    title: str | None,
    method: str,
) -> CarRow | None:
    vehicle = fields.get(FIELD_VEHICLE)
    brand = fields.get(FIELD_BRAND)
    model = fields.get(FIELD_MODEL)
    if vehicle:
        b, m = split_brand_model(vehicle)
        brand = brand or b
        # If "مدل" is a year, keep vehicle remainder as model_raw.
        if model and is_four_digit_year(model):
            extras = {**extras, "model_year": is_four_digit_year(model)}
            model = m or vehicle
        else:
            model = model or m or vehicle
    elif brand or model:
        if brand and model and is_four_digit_year(model):
            extras = {**extras, "model_year": is_four_digit_year(model)}
            _, m2 = split_brand_model(brand)
            model = m2 or brand
        elif not brand and model:
            brand, model = split_brand_model(model)
    else:
        b, m = split_brand_model(_vehicle_from_title(title))
        brand = b
        model = m

    # "مدل خودرو" mapped to vehicle; if it is clearly a full name, split.
    if brand is None and model:
        brand, model = split_brand_model(model)

    price_raw = fields.get(FIELD_PRICE)
    toman, unit = parse_price(price_raw, header_hint=price_header)
    extra_out = {k: v for k, v in extras.items() if v not in (None, "")}
    return CarRow(
        row_index=0,
        brand_raw=brand,
        model_raw=model,
        trim_raw=fields.get(FIELD_TRIM),
        colors_raw=fields.get(FIELD_COLOR),
        price_raw=price_raw,
        price_toman=toman,
        price_unit_guess=unit,
        plan_name_raw=fields.get(FIELD_PLAN),
        delivery_raw=fields.get(FIELD_DELIVERY),
        extra_json=extra_out,
        extract_method=method,
    )


def _dedupe_rows(rows: list[CarRow]) -> list[CarRow]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[CarRow] = []
    for row in rows:
        key = car_row_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
