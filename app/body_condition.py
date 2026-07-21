"""Extract, normalize, and classify listing body condition for bama / hm / karnameh."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

BodyConditionSummary = Literal["healthy", "non_healthy", "unknown"]

# Exact healthy labels per source (after whitespace trim; ZWNJ-insensitive match also applied).
_HEALTHY_EXACT_BY_SOURCE: dict[str, frozenset[str]] = {
    "bama": frozenset(
        {
            "بدون رنگ",
            "سالم",
            "سالم و بی‌خط و خش",
            "سالم و بیخط و خش",
            "بی‌خط و خش",
            "بیخط و خش",
            "بدون خط و خش",
        }
    ),
    "hm": frozenset({"سالم"}),
    "hamrah-mechanic": frozenset({"سالم"}),
    "karnameh": frozenset({"intact", "سالم"}),
}

_NON_HEALTHY_KEYWORDS: tuple[str, ...] = (
    "قطعه رنگ",
    "قطعه تعویض",
    "لکه رنگ",
    "چند لکه",
    "رنگ شده",
    "رنگ‌شده",
    "تعویض",
    "آسیب",
    "صافکاری",
    "رنگ دارد",
    "گلگیر",
    "گل پخ",
    "تصادف",
    "ضربه",
    "شکستگی",
    "پر رنگ",
    "فول رنگ",
    "کامل رنگ",
    "دور رنگ",
    "رنگ کامل",
)

_KARNAMEH_NON_HEALTHY_EN: dict[str, str] = {
    "painted": "partially_repainted",
    "scratched": "minor_scratches",
    "damaged": "minor_scratches",
}

_PERSIAN_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_PERSIAN_WORD_NUMBERS: dict[str, int] = {
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
}

_ZWNJ = "\u200c"


def normalize_body_condition_raw(value: str | None) -> str | None:
    """Trim and collapse whitespace in a body-condition label."""
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned or None


def _canonical_source(source: str) -> str:
    key = (source or "").strip().lower()
    if key == "hamrah-mechanic":
        return "hm"
    return key


def _compact_for_match(text: str) -> str:
    return text.replace(_ZWNJ, "").strip().lower()


def _is_healthy_exact(normalized: str, source: str) -> bool:
    source_key = _canonical_source(source)
    healthy_set = _HEALTHY_EXACT_BY_SOURCE.get(source_key, frozenset())
    compact = _compact_for_match(normalized)
    for candidate in healthy_set:
        if normalized == candidate or compact == _compact_for_match(candidate):
            return True
    if source_key == "karnameh" and compact == "intact":
        return True
    return False


def _contains_non_healthy_keyword(normalized: str) -> bool:
    for keyword in _NON_HEALTHY_KEYWORDS:
        if keyword not in normalized:
            continue
        if keyword == "تعویض" and "بدون" in normalized:
            continue
        return True

    if "خط و خش" in normalized and not re.search(r"بی[\u200c\-]?خط", normalized):
        return True

    return False


def classify_body_condition(raw: str | None, source: str) -> BodyConditionSummary:
    """
    Classify body condition as healthy / non_healthy / unknown.

    HM and Karnameh values are treated as definitive when raw text is present.
    Bama uses exact healthy labels plus keyword heuristics for painted/replaced panels.
    """
    normalized = normalize_body_condition_raw(raw)
    if not normalized:
        return "unknown"

    if _is_healthy_exact(normalized, source):
        return "healthy"

    if _contains_non_healthy_keyword(normalized):
        return "non_healthy"

    source_key = _canonical_source(source)
    compact = _compact_for_match(normalized)

    if source_key == "karnameh":
        for en_key in _KARNAMEH_NON_HEALTHY_EN:
            if en_key in compact:
                return "non_healthy"

    if source_key == "bama":
        if "رنگ" in normalized and "بدون رنگ" not in normalized:
            return "non_healthy"
        return "unknown"

    if source_key in {"hm", "hamrah-mechanic", "karnameh"}:
        return "non_healthy"

    return "unknown"


def _extract_painted_area_count(normalized: str) -> int | None:
    ascii_text = normalized.translate(_PERSIAN_DIGIT_MAP).lower()

    digit_match = re.search(r"(\d+)\s*(?:قطعه|لکه)\s*رنگ", ascii_text)
    if digit_match:
        return int(digit_match.group(1))

    digit_match = re.search(r"(\d+)\s*لکه", ascii_text)
    if digit_match:
        return int(digit_match.group(1))

    for word, number in _PERSIAN_WORD_NUMBERS.items():
        if re.search(rf"{word}\s*(?:قطعه|لکه)\s*رنگ", normalized):
            return number
        if re.search(rf"{word}\s*لکه", normalized):
            return number

    return None


def infer_body_condition_key_en(normalized: str | None, summary: BodyConditionSummary) -> str | None:
    """Map raw Persian/English body text to Seenad body_condition_key_en."""
    if summary == "healthy":
        return "perfect_condition"
    if summary != "non_healthy" or not normalized:
        return None

    compact = _compact_for_match(normalized)
    ascii_text = normalized.translate(_PERSIAN_DIGIT_MAP).lower()

    for en_key, mapped in _KARNAMEH_NON_HEALTHY_EN.items():
        if en_key in compact:
            return mapped

    if any(token in normalized for token in ("فول رنگ", "تمام رنگ", "تمام‌رنگ", "کامل رنگ", "fully repainted")):
        return "fully_repainted"

    if any(token in normalized for token in ("نیمه", "دور رنگ", "دوررنگ", "partially repainted", "partial")):
        return "partially_repainted"

    if "صافکاری" in normalized and "رنگ" not in normalized:
        return "paintless_dent_repair"

    painted_count = _extract_painted_area_count(normalized)
    if painted_count is not None:
        if painted_count <= 0:
            return "minor_scratches"
        if painted_count == 1:
            return "painted_1_area"
        if painted_count <= 10:
            return f"painted_{painted_count}_areas"
        return "painted_10_areas"

    if any(token in normalized for token in ("خط و خش", "خش")) or "scratch" in ascii_text:
        return "minor_scratches"

    if "رنگ" in normalized:
        return "minor_scratches"

    return "minor_scratches"


@dataclass(frozen=True)
class BodyConditionFields:
    body_condition_raw: str | None
    body_status: str | None
    body_condition: str | None
    body_condition_text: str | None
    body_condition_summary: BodyConditionSummary
    body_condition_key_en: str | None
    body_condition_is_healthy: bool | None
    body_condition_extracted_at: str
    body_condition_source_path: str | None


def build_body_condition_fields(
    *,
    source: str,
    raw: str | None,
    source_path: str | None,
    extracted_at: datetime | None = None,
) -> BodyConditionFields:
    """Build the raw_data body-condition contract fields for scraped_listings."""
    normalized = normalize_body_condition_raw(raw)
    summary = classify_body_condition(normalized, source)
    timestamp = (extracted_at or datetime.now(timezone.utc)).replace(microsecond=0)
    extracted_at_iso = timestamp.isoformat().replace("+00:00", "Z")
    key_en = infer_body_condition_key_en(normalized, summary)

    if summary == "healthy":
        display_text = normalized
        is_healthy: bool | None = True
    elif summary == "non_healthy":
        display_text = normalized
        is_healthy = False
    else:
        display_text = None
        key_en = None
        is_healthy = None

    return BodyConditionFields(
        body_condition_raw=normalized,
        body_status=display_text,
        body_condition=display_text,
        body_condition_text=display_text,
        body_condition_summary=summary,
        body_condition_key_en=key_en,
        body_condition_is_healthy=is_healthy,
        body_condition_extracted_at=extracted_at_iso,
        body_condition_source_path=source_path,
    )


def body_condition_fields_to_raw_data(fields: BodyConditionFields) -> dict[str, object]:
    """Serialize contract fields; omit unknown/unextracted summaries from payload."""
    if fields.body_condition_summary == "unknown":
        return {}
    return {
        "body_condition_raw": fields.body_condition_raw,
        "body_status": fields.body_status,
        "body_condition": fields.body_condition,
        "body_condition_text": fields.body_condition_text,
        "body_condition_summary": fields.body_condition_summary,
        "body_condition_key_en": fields.body_condition_key_en,
        "body_condition_is_healthy": fields.body_condition_is_healthy,
        "body_condition_extracted_at": fields.body_condition_extracted_at,
        "body_condition_source_path": fields.body_condition_source_path,
    }


def merge_body_condition_into_payload(
    payload: dict[str, object],
    *,
    source: str,
    raw: str | None,
    source_path: str | None,
    extracted_at: datetime | None = None,
) -> dict[str, object]:
    fields = build_body_condition_fields(
        source=source,
        raw=raw,
        source_path=source_path,
        extracted_at=extracted_at,
    )
    return {**payload, **body_condition_fields_to_raw_data(fields)}
