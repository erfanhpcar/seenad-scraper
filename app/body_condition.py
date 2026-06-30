"""Extract, normalize, and classify listing body condition for bama / hm / karnameh."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

BodyConditionSummary = Literal["healthy", "non_healthy", "unknown"]

HEALTHY_BODY_STATUS_FA = "سالم و بی‌خط و خش"

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
    "karnameh": frozenset({"intact"}),
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
    if source_key == "bama":
        if "رنگ" in normalized and "بدون رنگ" not in normalized:
            return "non_healthy"
        return "unknown"

    if source_key in {"hm", "hamrah-mechanic", "karnameh"}:
        return "non_healthy"

    return "unknown"


@dataclass(frozen=True)
class BodyConditionFields:
    body_condition_raw: str | None
    body_status: str | None
    body_condition: str | None
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

    if summary == "healthy":
        body_status = HEALTHY_BODY_STATUS_FA
        key_en: str | None = "perfect_condition"
        is_healthy: bool | None = True
    elif summary == "non_healthy":
        body_status = normalized
        key_en = None
        is_healthy = False
    else:
        body_status = None
        key_en = None
        is_healthy = None

    return BodyConditionFields(
        body_condition_raw=normalized,
        body_status=body_status,
        body_condition=normalized,
        body_condition_summary=summary,
        body_condition_key_en=key_en,
        body_condition_is_healthy=is_healthy,
        body_condition_extracted_at=extracted_at_iso,
        body_condition_source_path=source_path,
    )


def body_condition_fields_to_raw_data(fields: BodyConditionFields) -> dict[str, object]:
    return {
        "body_condition_raw": fields.body_condition_raw,
        "body_status": fields.body_status,
        "body_condition": fields.body_condition,
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
