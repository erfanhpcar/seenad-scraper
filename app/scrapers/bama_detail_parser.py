"""Fetch a single Bama listing HTML from __INITIAL_STATE__, ld+json, or Nuxt __NUXT_DATA__."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from app.body_condition import normalize_body_condition_raw
from app.scrapers.bama_scraper import (
    USER_AGENTS,
    _extract_external_id,
    _extract_int,
    _extract_price,
)

LOGGER = logging.getLogger(__name__)

BAMA_DOMAIN = "https://bama.ir"
_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_YEAR_FROM_URL_END_RE = re.compile(r"(?:[-_/]|%2[fF]|%2[dD])(1[34]\d{2}|19\d{2}|20\d{2})(?:[/#?]|$)")
_YEAR_FROM_TEXT_RE = re.compile(r"(?<!\d)(1[34]\d{2}|19\d{2}|20\d{2})(?!\d)")
_MODEL_YEAR_MIN = 1350
_MODEL_YEAR_MAX = 1405

_REACTIVE_WRAPPERS = frozenset({"Reactive", "ShallowReactive", "Ref"})
_TIMESTAMP_KEYS_EXACT = frozenset(
    {
        "published_at_date",
        "published_at",
        "created_at",
        "modified_at",
        "updated_at",
        "publish_date",
        "publishdate",
        "publication_date",
        "publishedat",
        "createdat",
        "modifiedat",
        "updatedat",
        "date_published",
        "datepublished",
        "datemodified",
        "date_modified",
    }
)


def _normalize_digit_chars(text: str) -> str:
    """Persian/Arabic digits → ASCII; strip separators and RTL marks."""
    t = text.translate(_FA_AR_TO_EN_DIGITS).strip()
    for ch in ("،", ",", "٬", " ", "\u200c", "\u200f", "\u202b", "\u202c"):
        t = t.replace(ch, "")
    return t


def _normalize_model_year(year: int | None) -> int | None:
    if year is None:
        return None
    year = int(year)
    if year > 1800:
        year = year - 621
    if _MODEL_YEAR_MIN <= year <= _MODEL_YEAR_MAX:
        return year
    return None


def _extract_model_year_candidates_from_text(text: str | None) -> int | None:
    if not text:
        return None
    normalized = _normalize_digit_chars(text)
    for match in _YEAR_FROM_TEXT_RE.finditer(normalized):
        year = _normalize_model_year(int(match.group(1)))
        if year is not None:
            return year
    return None


def _extract_model_year_from_url_tail(url_text: str | None) -> int | None:
    if not url_text:
        return None
    normalized = _normalize_digit_chars(url_text)
    decoded = unquote(normalized)
    m = _YEAR_FROM_URL_END_RE.search(decoded)
    if m:
        year = _normalize_model_year(int(m.group(1)))
        if year is not None:
            return year

    # Newer URL slugs may place the year in the middle instead of URL tail.
    for match in _YEAR_FROM_TEXT_RE.finditer(decoded):
        year = _normalize_model_year(int(match.group(1)))
        if year is not None:
            return year
    return None


def _looks_like_persian_relative_time(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    hints = (
        "پیش",
        "لحظاتی",
        "دقایقی",
        "ساعتی",
        "امروز",
        "دیروز",
        " دقیقه",
        " ساعت",
        " روز",
    )
    return any(h in lowered for h in hints)


def _parse_persian_relative_time_to_iso(text: str, *, now: datetime | None = None) -> str | None:
    """
    Convert Persian relative time text (e.g. "دقایقی پیش", "۲ ساعت پیش") to ISO 8601.
    """
    current = (now or datetime.now().astimezone()).replace(microsecond=0)
    raw = text.strip()
    if not raw:
        return None
    normalized = _normalize_digit_chars(raw).lower()

    if "لحظاتی پیش" in normalized or "دقایقی پیش" in normalized or "چند دقیقه پیش" in normalized:
        return (current - timedelta(minutes=2)).isoformat()
    if "ساعتی پیش" in normalized:
        return (current - timedelta(hours=1)).isoformat()

    minute_match = re.search(r"(\d{1,3})\s*دقیقه(?:\s*ای)?\s*پیش", normalized)
    if minute_match:
        minutes = int(minute_match.group(1))
        return (current - timedelta(minutes=minutes)).isoformat()

    hour_match = re.search(r"(\d{1,3})\s*ساعت(?:ی)?\s*پیش", normalized)
    if hour_match:
        hours = int(hour_match.group(1))
        return (current - timedelta(hours=hours)).isoformat()

    day_match = re.search(r"(\d{1,3})\s*روز\s*پیش", normalized)
    if day_match:
        days = int(day_match.group(1))
        return (current - timedelta(days=days)).isoformat()

    today_time = re.search(r"امروز\s*(\d{1,2}):(\d{1,2})", normalized)
    if today_time:
        hh, mm = int(today_time.group(1)), int(today_time.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return current.replace(hour=hh, minute=mm, second=0).isoformat()

    yday_time = re.search(r"دیروز\s*(\d{1,2}):(\d{1,2})", normalized)
    if yday_time:
        hh, mm = int(yday_time.group(1)), int(yday_time.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            dt = (current - timedelta(days=1)).replace(hour=hh, minute=mm, second=0)
            return dt.isoformat()

    if "دیروز" in normalized:
        return (current - timedelta(days=1)).isoformat()
    if "امروز" in normalized:
        return current.isoformat()

    return None


def _to_iso8601_timestamp(value: Any, *, now: datetime | None = None) -> str | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        # heuristic: timestamps greater than 1e12 are likely milliseconds
        seconds = numeric / 1000 if numeric > 1_000_000_000_000 else numeric
        min_epoch = 946684800  # 2000-01-01T00:00:00Z
        max_epoch = (datetime.now().astimezone() + timedelta(days=2)).timestamp()
        if seconds < min_epoch or seconds > max_epoch:
            return None
        try:
            return datetime.fromtimestamp(seconds).astimezone().replace(microsecond=0).isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    if _looks_like_persian_relative_time(raw):
        relative = _parse_persian_relative_time_to_iso(raw, now=now)
        if relative:
            return relative

    normalized = _normalize_digit_chars(raw)
    if normalized.isdigit():
        return _to_iso8601_timestamp(int(normalized), now=now)

    candidate = raw.strip().replace("Z", "+00:00")
    for fmt in (
        None,
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(candidate)
            else:
                dt = datetime.strptime(candidate, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return dt.astimezone().replace(microsecond=0).isoformat()
        except ValueError:
            continue
    return None


def _find_timestamp_candidate(node: Any) -> tuple[str | None, str]:
    """
    Recursively find a publication-related timestamp in hidden page data.
    """
    best_iso: str | None = None
    best_source = ""
    best_score = -1

    def consider(key: str, val: Any, path: str) -> None:
        nonlocal best_iso, best_source, best_score
        iso = _to_iso8601_timestamp(val)
        if not iso:
            return
        key_norm = key.strip().lower()
        if key_norm in _TIMESTAMP_KEYS_EXACT:
            score = 100
        elif any(k in key_norm for k in ("publish", "published", "create", "created", "modify", "modified", "update", "date", "time")):
            score = 50
        else:
            score = 10
        if score > best_score:
            best_score = score
            best_iso = iso
            best_source = path

    def walk(current: Any, path: str) -> None:
        if isinstance(current, dict):
            for k, v in current.items():
                key_text = str(k)
                next_path = f"{path}.{key_text}" if path else key_text
                consider(key_text, v, next_path)
                walk(v, next_path)
        elif isinstance(current, list):
            for idx, item in enumerate(current):
                walk(item, f"{path}[{idx}]")

    walk(node, "")
    return best_iso, best_source


def _extract_created_at_source_from_hidden_data(html: str, listing_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        if "__NUXT_DATA__" in raw or script.get("id") == "__NUXT_DATA__":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                try:
                    root = _nuxt_hydrate(payload)
                    iso, source = _find_timestamp_candidate(root)
                    if iso:
                        LOGGER.info("bama_detail_parser created_at from __NUXT_DATA__ path=%s", source)
                        return iso
                except Exception:
                    LOGGER.debug("Failed to inspect __NUXT_DATA__ timestamp fields", exc_info=True)

    try:
        state = extract_initial_state(html)
        iso, source = _find_timestamp_candidate(state)
        if iso:
            LOGGER.info("bama_detail_parser created_at from __INITIAL_STATE__ path=%s", source)
            return iso
    except ValueError:
        pass

    # Fallback: relative Persian labels that may be rendered in visible HTML.
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    for pattern in (
        r"(?:لحظاتی پیش|دقایقی پیش|ساعتی پیش|چند دقیقه پیش)",
        r"[\d۰-۹٠-٩]{1,3}\s*دقیقه(?:\s*ای)?\s*پیش",
        r"[\d۰-۹٠-٩]{1,3}\s*ساعت(?:ی)?\s*پیش",
        r"[\d۰-۹٠-٩]{1,3}\s*روز\s*پیش",
        r"(?:امروز|دیروز)\s*[\d۰-۹٠-٩]{1,2}:[\d۰-۹٠-٩]{1,2}",
        r"(?:امروز|دیروز)\s*(?:ساعت)?\s*[\d۰-۹٠-٩]{1,2}:[\d۰-۹٠-٩]{1,2}",
        r"(?:امروز|دیروز)",
    ):
        match = re.search(pattern, text)
        if match:
            matched_text = match.group(0)
            iso = _to_iso8601_timestamp(matched_text)
            if iso:
                LOGGER.info("bama_detail_parser created_at from relative text match=%r", matched_text)
                return iso

    # Last-resort fallback: scan raw HTML/inline JSON strings for any relative-time phrase.
    raw_time_matches = re.findall(
        r"(?:لحظاتی پیش|دقایقی پیش|ساعتی پیش|چند دقیقه پیش|امروز|دیروز|[\d۰-۹٠-٩]{1,3}\s*(?:دقیقه|ساعت|روز)(?:\s*ای)?\s*پیش)",
        html,
    )
    for candidate in raw_time_matches:
        iso = _to_iso8601_timestamp(candidate)
        if iso:
            LOGGER.info("bama_detail_parser created_at from HTML text candidate=%r", candidate)
            return iso

    LOGGER.debug("bama_detail_parser created_at not found in hidden data url=%s", listing_url)
    return None


def price_string_to_irr_integer(raw: str | None, *, assume_toman_if_no_unit: bool = False) -> int | None:
    """
    Parse a human price string into IRR integer (ریال).
    If 'تومان' appears → multiply digit-run by 10. If 'ریال' → digits as-is.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    lower = text.lower()
    is_toman = "تومان" in text or "تومنی" in text
    is_rial = "ریال" in text or "irr" in lower
    normalized = _normalize_digit_chars(text)
    digits = re.sub(r"[^\d]", "", normalized)
    if not digits:
        return None
    try:
        base = int(digits)
    except ValueError:
        return None
    if is_toman:
        return base * 10
    if is_rial:
        return base
    if assume_toman_if_no_unit:
        return base * 10
    return base


def irr_integer_to_decimal(irr: int | None) -> Decimal | None:
    if irr is None or irr <= 0:
        return None
    if irr >= 1_000_000_000_000:
        LOGGER.warning("bama price IRR too large for Numeric(14,2) column: %s", irr)
        return None
    return Decimal(irr)


def _mileage_from_persian_text(text: str | None) -> int | None:
    if not text:
        return None
    compact = text.replace(" ", "").replace("\u200c", "")
    if "صفر" in compact:
        return 0
    return _extract_int(text)


def _detail_code_from_url(listing_url: str) -> str | None:
    m = re.search(r"/detail-([^/-]+)", listing_url)
    return m.group(1).strip() if m else None


def _nuxt_hydrate(payload: list[Any]) -> dict[str, Any]:
    """
    Hydrate Nuxt/evonex dehydrated payload array (cf. Rich Harris devalue): primitives stored in cells
    stay literal; integers encountered inside nested dict/list structures dereference slots by index.
    """
    hydrated: dict[int, Any] = {}
    pending = object()

    def hydrate(index: int) -> Any:
        if index < 0 or index >= len(payload):
            raise IndexError(f"NUXT payload index out of range: {index}")
        if index in hydrated:
            prev = hydrated[index]
            return None if prev is pending else prev
        hydrated[index] = pending
        cell = payload[index]
        try:
            result = revive_cell(cell)
        finally:
            hydrated[index] = result
        return result

    def revive_nested_int(idx: int) -> Any:
        if idx < 0 or idx >= len(payload):
            return idx
        return hydrate(idx)

    def revive_cell(cell: Any) -> Any:
        if cell is None:
            return None
        if isinstance(cell, bool):
            return cell
        if isinstance(cell, str):
            return cell
        if isinstance(cell, float):
            return cell
        if isinstance(cell, int):
            return cell
        if isinstance(cell, list):
            if (
                len(cell) == 2
                and isinstance(cell[0], str)
                and cell[0] in _REACTIVE_WRAPPERS
                and isinstance(cell[1], int)
            ):
                return hydrate(cell[1])
            out: list[Any] = []
            for item in cell:
                if item is None or isinstance(item, bool):
                    out.append(item)
                elif isinstance(item, str):
                    out.append(item)
                elif isinstance(item, float):
                    out.append(item)
                elif isinstance(item, int):
                    out.append(revive_nested_int(item))
                elif isinstance(item, list):
                    out.append(revive_cell(item))
                elif isinstance(item, dict):
                    out.append(revive_cell(item))
                else:
                    out.append(item)
            return out
        if isinstance(cell, dict):
            resolved: dict[str, Any] = {}
            for key, val in cell.items():
                sk = str(key)
                if isinstance(val, bool) or val is None:
                    resolved[sk] = val
                elif isinstance(val, str):
                    resolved[sk] = val
                elif isinstance(val, float):
                    resolved[sk] = val
                elif isinstance(val, int):
                    resolved[sk] = revive_nested_int(val)
                elif isinstance(val, list):
                    resolved[sk] = revive_cell(val)
                elif isinstance(val, dict):
                    resolved[sk] = revive_cell(val)
                else:
                    resolved[sk] = val
            return resolved
        return cell

    root = hydrate(1)
    if not isinstance(root, dict):
        raise ValueError("NUXT payload root is not an object")
    return root


def _try_get_nuxt_pdp_inner(html: str, listing_url: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NUXT_DATA__")
    raw = tag.string if tag else None
    if not raw or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("Could not JSON-parse __NUXT_DATA__ script.")
        return None
    if not isinstance(payload, list):
        return None
    try:
        root = _nuxt_hydrate(payload)
    except Exception:
        LOGGER.warning("NUXT hydrate failed", exc_info=True)
        return None
    data_block = root.get("data")
    if not isinstance(data_block, dict):
        return None
    code = _detail_code_from_url(listing_url)
    preferred_key = f"get-ad-pdp-car_{code}" if code else None
    if preferred_key and preferred_key in data_block:
        chosen = data_block[preferred_key]
    else:
        chosen = None
        for key, val in data_block.items():
            if isinstance(key, str) and key.startswith("get-ad-pdp-car_"):
                chosen = val
                break
    if not isinstance(chosen, dict):
        return None
    inner = chosen.get("data")
    return inner if isinstance(inner, dict) else None


def _extract_price_irr_from_nuxt_price_obj(price_obj: Any) -> tuple[int | None, bool, str]:
    """Returns (irr_int_or_none, is_negotiable, debug_note)."""
    if not isinstance(price_obj, dict):
        return None, False, ""
    ptype_raw = price_obj.get("type")
    ptype = str(ptype_raw).lower() if ptype_raw is not None else ""

    if ptype in {"negotiable", "negotiation"}:
        return None, True, "nuxt_price.type=negotiable"

    cp = price_obj.get("collaborationPrice")
    if isinstance(cp, int) and cp > 0:
        return cp, False, "nuxt_price.collaborationPrice"

    fixed = price_obj.get("fixed")
    if isinstance(fixed, dict):
        val = fixed.get("value")
        if isinstance(val, int) and val > 0:
            return val, False, "nuxt_price.fixed.value_irr"
        text_key = fixed.get("textKey")
        txt = fixed.get("text")
        if isinstance(txt, str):
            assume_toman = isinstance(text_key, str) and "toman" in text_key.lower()
            parsed = price_string_to_irr_integer(txt, assume_toman_if_no_unit=assume_toman)
            if parsed:
                return parsed, False, f"nuxt_price.fixed.text->{parsed}_irr ({text_key})"

    inst = price_obj.get("installment")
    if isinstance(inst, dict):
        iv = inst.get("value") or inst.get("totalPrice") or inst.get("amount")
        if isinstance(iv, int) and iv > 0:
            return iv, False, "nuxt_price.installment"

    presale = price_obj.get("presale")
    if isinstance(presale, dict):
        pv = presale.get("value") if isinstance(presale.get("value"), int) else None
        if isinstance(pv, int) and pv > 0:
            return pv, False, "nuxt_price.presale"

    return None, False, ""


def _city_from_nuxt_location(inner: dict[str, Any]) -> str | None:
    loc = inner.get("location")
    if not isinstance(loc, dict):
        loc = inner.get("content", {})
        if isinstance(loc, dict):
            loc = loc.get("location")
    if not isinstance(loc, dict):
        return None
    val = loc.get("value")
    if isinstance(val, dict):
        city = val.get("city")
        if isinstance(city, str) and city.strip():
            return city.strip()
        region = val.get("region")
        province = val.get("province")
        parts = [p for p in (province, region) if isinstance(p, str) and p.strip()]
        if parts:
            return parts[0].strip()
    txt = loc.get("text")
    if isinstance(txt, str) and txt.strip():
        head = txt.split("،")[0].strip()
        return head or txt.strip()
    return None


def _trim_text_implies_gearbox(tx: str) -> bool:
    """Avoid using numeric-only trims (e.g. BMW '28') as gearbox labels."""
    if len(tx.strip()) < 5:
        return False
    lowered = tx.lower()
    gearbox_hints = (
        "دنده",
        "اتوماتیک",
        "cvt",
        "dct",
        "tiptronic",
        "گیربکس",
        "manual",
        "automatic",
    )
    return any(h in tx or h in lowered for h in gearbox_hints)


def _body_status_from_nuxt_vehicle(vehicle: dict[str, Any]) -> tuple[str | None, str | None]:
    body_status = vehicle.get("bodyStatus")
    if isinstance(body_status, dict):
        for key in ("value", "text"):
            raw = body_status.get(key)
            if isinstance(raw, str) and raw.strip():
                normalized = normalize_body_condition_raw(raw)
                if normalized:
                    return normalized, f"vehicle.bodyStatus.{key}"
    return None, None


def _body_status_from_nuxt_inner(inner: dict[str, Any]) -> tuple[str | None, str | None]:
    veh = _extract_vehicle_bundle(inner)
    value, path = _body_status_from_nuxt_vehicle(veh)
    if value:
        return value, path

    specs = veh.get("specs") if isinstance(veh.get("specs"), dict) else {}
    details = specs.get("details") if isinstance(specs.get("details"), dict) else {}
    for key in ("bodyStatus", "body_status", "bodyCondition", "body_condition"):
        blob = details.get(key)
        if isinstance(blob, dict):
            for sub_key in ("value", "text"):
                raw = blob.get(sub_key)
                if isinstance(raw, str) and raw.strip():
                    normalized = normalize_body_condition_raw(raw)
                    if normalized:
                        return normalized, f"vehicle.specs.details.{key}.{sub_key}"
        elif isinstance(blob, str) and blob.strip():
            normalized = normalize_body_condition_raw(blob)
            if normalized:
                return normalized, f"vehicle.specs.details.{key}"
    return None, None


def _body_status_from_visible_html(html: str) -> tuple[str | None, str | None]:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    for pattern in (
        r"وضعیت بدنه\s*[:：]?\s*([^\n\r|]+?)(?=\s*(?:رنگ بدنه|رنگ داخل|گیربکس|کارکرد|شهر|توضیح|$))",
        r"وضعیت بدنه\s*[:：]?\s*([^\n\r|]+)",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        normalized = normalize_body_condition_raw(match.group(1))
        if normalized:
            return normalized, "html:text:وضعیت بدنه"
    return None, None


def _gearbox_label_from_nuxt_vehicle(vehicle: dict[str, Any]) -> str | None:
    specs = vehicle.get("specs") if isinstance(vehicle.get("specs"), dict) else {}
    details = specs.get("details") if isinstance(specs.get("details"), dict) else {}
    for key in ("gearbox", "transmission", "gear"):
        blob = details.get(key)
        if isinstance(blob, dict):
            v = blob.get("value") or blob.get("text")
            if isinstance(v, str) and v.strip():
                return v.strip()
    shaft = details.get("driveShaft")
    if isinstance(shaft, dict):
        v = shaft.get("value") or shaft.get("text")
        if isinstance(v, str) and v.strip():
            return v.strip()
    trim = vehicle.get("trim")
    if isinstance(trim, dict):
        tx = trim.get("text")
        if isinstance(tx, str) and tx.strip() and _trim_text_implies_gearbox(tx):
            return tx.strip()
    return None


def _extract_vehicle_bundle(inner: dict[str, Any]) -> dict[str, Any]:
    vehicle = inner.get("vehicle")
    return vehicle if isinstance(vehicle, dict) else {}


def _extract_fields_from_nuxt_inner(inner: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "brand": None,
        "model": None,
        "trim": None,
        "year": None,
        "mileage": None,
        "color": None,
        "gearbox_text": None,
        "city": None,
        "price_irr": None,
        "negotiable": False,
        "price_note": "",
    }
    veh = _extract_vehicle_bundle(inner)
    brand = veh.get("brand")
    if isinstance(brand, dict):
        bt = brand.get("text")
        if isinstance(bt, str) and bt.strip():
            out["brand"] = bt.strip()
    model = veh.get("model")
    if isinstance(model, dict):
        mt = model.get("text")
        if isinstance(mt, str) and mt.strip():
            out["model"] = mt.strip().strip()
    trim = veh.get("trim")
    if isinstance(trim, dict):
        tt = trim.get("text")
        if isinstance(tt, str) and tt.strip():
            out["trim"] = tt.strip()
    year_blob = veh.get("year")
    if isinstance(year_blob, dict):
        yv = year_blob.get("value")
        if isinstance(yv, int):
            out["year"] = yv

    mileage_blob = veh.get("mileage")
    if isinstance(mileage_blob, dict):
        mv = mileage_blob.get("value")
        if isinstance(mv, int) and mv >= 0:
            out["mileage"] = mv
        else:
            mt = mileage_blob.get("text")
            parsed_m = _mileage_from_persian_text(mt if isinstance(mt, str) else None)
            if parsed_m is not None:
                out["mileage"] = parsed_m

    color_blob = veh.get("color")
    if isinstance(color_blob, dict):
        body = color_blob.get("body")
        if isinstance(body, dict):
            cv = body.get("value")
            if isinstance(cv, str) and cv.strip():
                out["color"] = cv.strip()

    gb = _gearbox_label_from_nuxt_vehicle(veh)
    if gb:
        out["gearbox_text"] = gb

    city = _city_from_nuxt_location(inner)
    if city:
        out["city"] = city

    irr, neg, note = _extract_price_irr_from_nuxt_price_obj(inner.get("price"))
    out["price_irr"] = irr
    out["negotiable"] = neg
    out["price_note"] = note
    return out


def _regex_price_irr_from_html(html: str) -> tuple[int | None, str]:
    """Fallback: Persian/English digit runs near تومان / ریال."""
    candidates: list[tuple[int, str]] = []

    for m in re.finditer(
        r"([\d۰-۹،٬,_]{5,22})\s*(تومان|ریال)",
        html,
    ):
        chunk, unit = m.group(1), m.group(2)
        irr = price_string_to_irr_integer(f"{chunk} {unit}")
        if irr and irr >= 10_000:
            candidates.append((irr, f"regex_near_{unit}"))

    if not candidates:
        for m in re.finditer(r'"price"\s*:\s*"(\d{7,})"', html):
            irr = int(m.group(1))
            if irr >= 10_000:
                candidates.append((irr, "regex_ld_price_string"))

    if not candidates:
        return None, ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_irr, tag = candidates[0]
    return best_irr, tag


def _html_marked_negotiable(html: str) -> bool:
    if "توافقی" in html:
        return True
    if re.search(r'"type"\s*:\s*"negotiable"', html):
        return True
    if re.search(r',\s*"negotiable"\s*,', html):
        return True
    return False


def _offer_price_irr_integer(offers: Any) -> tuple[int | None, str]:
    """schema.org Offer.price → IRR int."""
    if isinstance(offers, list):
        for offer in offers:
            irr, note = _offer_price_irr_integer(offer)
            if irr:
                return irr, note
        return None, ""
    if not isinstance(offers, dict):
        return None, ""
    raw = offers.get("price")
    if raw is None:
        return None, ""
    if isinstance(raw, bool):
        return None, ""
    if isinstance(raw, int):
        return (raw if raw > 0 else None), "ld+json_offer.price_int"
    if isinstance(raw, float) and raw.is_integer():
        ri = int(raw)
        return (ri if ri > 0 else None), "ld+json_offer.price_float"
    if isinstance(raw, str):
        irr = price_string_to_irr_integer(raw)
        return (irr, "ld+json_offer.price_str") if irr else (None, "")
    return None, ""


@dataclass(frozen=True)
class BamaDetailExtract:
    external_id: str
    brand: str | None
    model: str | None
    trim: str | None
    year: int | None
    mileage: int | None
    color: str | None
    transmission: str | None
    price: Decimal | None
    description: str | None
    images: tuple[str, ...]
    listing_url: str
    city: str | None = None
    price_is_negotiable: bool = False
    price_debug_note: str = ""
    created_at_source: str | None = None
    body_condition: str | None = None
    body_condition_source_path: str | None = None


def _dynamic_referer(listing_url: str) -> str:
    parsed = urlparse(listing_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/car"
    return f"{BAMA_DOMAIN}/car"


def _random_headers(listing_url: str) -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": _dynamic_referer(listing_url),
        "Connection": "keep-alive",
    }


def fetch_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{BAMA_DOMAIN}/{normalized.lstrip('/')}"

    time.sleep(random.uniform(3, 7))
    response = requests.get(normalized, headers=_random_headers(normalized), timeout=timeout_seconds)
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _parse_json_parse_string_assignment(remainder: str) -> dict[str, Any] | None:
    """
    Handle patterns like JSON.parse("{\"key\":...}") by extracting the quoted payload,
    decoding escapes, then loading JSON.
    """
    stripped = remainder.lstrip()
    if not stripped.startswith("JSON.parse"):
        return None

    match_paren = re.match(r"JSON\.parse\s*\(\s*", stripped)
    if not match_paren:
        return None
    rest = stripped[match_paren.end() :]

    quote = rest[:1]
    if quote not in {'"', "'"}:
        return None
    rest = rest[1:]
    chars: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            esc = rest[i + 1]
            escape_map = {'n': '\n', 'r': '\r', 't': '\t', '"': '"', "'": "'", '\\': '\\'}
            chars.append(escape_map.get(esc, esc))
            i += 2
            continue
        if ch == quote:
            break
        chars.append(ch)
        i += 1
    literal = "".join(chars)
    try:
        loaded = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_state_assignment(script_body: str) -> dict[str, Any] | None:
    marker = "__INITIAL_STATE__"
    idx = script_body.find(marker)
    if idx == -1:
        return None

    tail = script_body[idx:]
    eq = tail.find("=")
    if eq == -1:
        return None

    remainder = tail[eq + 1 :].strip()

    parsed_literal = _parse_json_parse_string_assignment(remainder)
    if parsed_literal is not None:
        return parsed_literal

    decoder = json.JSONDecoder()
    brace = remainder.find("{")
    if brace == -1:
        return None
    try:
        data, _ = decoder.raw_decode(remainder[brace:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_initial_state(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        raw = script.string
        if not raw:
            raw = script.get_text()
        if not raw or "__INITIAL_STATE__" not in raw:
            continue
        parsed = _parse_state_assignment(raw)
        if parsed:
            return parsed
    raise ValueError("Could not parse __INITIAL_STATE__ JSON from page scripts")


def _types_match(types_val: Any, want: str) -> bool:
    low = want.lower()
    if isinstance(types_val, str):
        return types_val.lower() == low
    if isinstance(types_val, list):
        return any(isinstance(t, str) and t.lower() == low for t in types_val)
    return False


def _flatten_ld_json_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            flat: list[dict[str, Any]] = []
            for item in graph:
                flat.extend(_flatten_ld_json_payload(item))
            return flat
        return [payload]
    if isinstance(payload, list):
        flat = []
        for item in payload:
            flat.extend(_flatten_ld_json_payload(item))
        return flat
    return []


def _extract_from_schema_org_ld_json(html: str, listing_url: str) -> BamaDetailExtract | None:
    """
    New Bama detail pages use Nuxt/evonex and expose a schema.org Product in application/ld+json.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        ctype = (script.get("type") or "").lower()
        if "ld+json" not in ctype:
            continue
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _flatten_ld_json_payload(payload):
            if not isinstance(obj, dict) or not _types_match(obj.get("@type"), "Product"):
                continue

            brand_blob = obj.get("brand")
            brand_name: str | None = None
            if isinstance(brand_blob, dict):
                brand_name = _first_non_empty_str(brand_blob, ("name",))
            elif isinstance(brand_blob, str):
                brand_name = brand_blob.strip() or None

            title = _first_non_empty_str(obj, ("name",))
            description = _first_non_empty_str(obj, ("description",))

            offers_raw = obj.get("offers")
            price_irr, offer_price_note = _offer_price_irr_integer(offers_raw)
            price_val = irr_integer_to_decimal(price_irr)

            item_car: dict[str, Any] | None = None
            if isinstance(offers_raw, dict):
                io = offers_raw.get("itemOffered")
                item_car = io if isinstance(io, dict) else None
            elif isinstance(offers_raw, list):
                for off in offers_raw:
                    if isinstance(off, dict):
                        io = off.get("itemOffered")
                        if isinstance(io, dict):
                            item_car = io
                            break

            ext_from_car = ""
            year_int: int | None = None
            transmission: str | None = None
            color_car: str | None = None
            if item_car:
                ident = item_car.get("identifier")
                if ident is not None:
                    ext_from_car = str(ident).strip()
                pd = item_car.get("productionDate") or item_car.get("vehicleModelDate")
                if pd is not None:
                    year_int = _extract_int(str(pd))
                transmission = _first_non_empty_str(item_car, ("vehicleTransmission", "transmission"))
                color_car = _first_non_empty_str(item_car, ("color",))

            color_top = _first_non_empty_str(obj, ("color",))
            color = color_top or color_car

            from_url = _extract_external_id(listing_url, fallback="").strip()
            external_id = (ext_from_car or from_url or "unknown").strip() or "unknown"

            img_raw = obj.get("image")
            images = tuple(dict.fromkeys(_collect_images(img_raw)))

            LOGGER.info(
                "bama_detail_parser ld+json Product matched external_id=%r price=%s (%s) brand=%r title=%r",
                external_id,
                price_val,
                offer_price_note,
                brand_name,
                title,
            )

            return BamaDetailExtract(
                external_id=external_id,
                brand=brand_name,
                model=title,
                trim=None,
                year=year_int,
                mileage=None,
                color=color,
                transmission=transmission,
                price=price_val,
                description=description,
                images=images,
                listing_url=listing_url,
                price_debug_note=f"ld+json:{offer_price_note}" if offer_price_note else "ld+json",
            )

    return None


def _first_non_empty_str(obj: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = obj.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            text = str(int(val)) if isinstance(val, float) and val.is_integer() else str(val)
        else:
            text = str(val).strip()
        if text:
            return text
    return None


def _collect_images(value: Any) -> list[str]:
    urls: list[str] = []

    def push(u: Any) -> None:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)

    if isinstance(value, str):
        push(value)
        return urls
    if isinstance(value, list):
        for item in value:
            urls.extend(_collect_images(item))
        return urls
    if isinstance(value, dict):
        if "items" in value:
            return _collect_images(value.get("items"))
        for key in ("largeUrl", "url", "src", "imageUrl"):
            push(value.get(key))
        nested = value.get("images") or value.get("photos") or value.get("gallery")
        if nested is not None:
            urls.extend(_collect_images(nested))
        return urls
    return urls


def _listing_candidate_score(obj: dict[str, Any]) -> int:
    keys_lower = {str(k).lower() for k in obj.keys()}
    score = 0
    brand_aliases = {"brand", "brandtitle", "brand_name", "brandname"}
    model_aliases = {"model", "modeltitle", "model_name", "modelname"}
    trim_aliases = {"trim", "trimtitle", "varianttitle", "detailtrim", "variant"}
    detail_aliases = {"detail", "listing", "advertisement", "ad", "card"}

    if keys_lower & brand_aliases:
        score += 3
    if keys_lower & model_aliases:
        score += 3
    if keys_lower & trim_aliases:
        score += 2
    price_aliases = {"price", "pricetitle", "amount", "detailprice"}
    mileage_aliases = {"mileage", "mileagetitle", "distance", "kilonmeter", "kilometer"}
    if keys_lower & price_aliases:
        score += 2
    if keys_lower & mileage_aliases:
        score += 2
    if keys_lower & detail_aliases:
        score += 1
    return score


def _best_listing_blob(state: dict[str, Any]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_score = -1

    def walk(node: Any) -> None:
        nonlocal best, best_score
        if isinstance(node, dict):
            score = _listing_candidate_score(node)
            if score > best_score:
                best = node
                best_score = score
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(state)
    return best if best is not None else state


def _listing_id_from_blob(blob: dict[str, Any]) -> str | None:
    """Pick a stable listing id from __INITIAL_STATE__ object keys used by Bama variants."""
    for key in (
        "id",
        "listingId",
        "listing_id",
        "adId",
        "ad_id",
        "advertisementId",
        "advertisement_id",
        "detailId",
        "detail_id",
        "code",
        "slug",
    ):
        val = blob.get(key)
        if val is None or isinstance(val, bool):
            continue
        if isinstance(val, int):
            return str(val)
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        text = str(val).strip()
        if text:
            return text
    return None


def _extract_fields(blob: dict[str, Any]) -> dict[str, Any]:
    brand = _first_non_empty_str(
        blob,
        ("brandTitle", "brand", "brandName", "brand_name"),
    )
    model = _first_non_empty_str(
        blob,
        ("modelTitle", "model", "modelName", "model_name"),
    )
    trim = _first_non_empty_str(
        blob,
        ("trimTitle", "trim", "variantTitle", "vehicleConfiguration", "variant"),
    )

    year_raw = _first_non_empty_str(blob, ("yearTitle", "year", "productionYear", "modelYear"))
    year_int = _extract_int(year_raw) if year_raw else None

    mileage_raw = _first_non_empty_str(blob, ("mileageTitle", "mileage", "distance", "kilometer"))
    mileage_int = _extract_int(mileage_raw) if mileage_raw else None

    color = _first_non_empty_str(blob, ("colorTitle", "color", "bodyColor"))

    transmission = _first_non_empty_str(
        blob,
        ("gearboxTitle", "gearbox", "transmission", "gearTitle"),
    )

    price_raw = _first_non_empty_str(blob, ("priceTitle", "price", "amount", "detailPrice"))
    price_val = _extract_price(price_raw) if price_raw else None

    description = _first_non_empty_str(
        blob,
        ("description", "detailDescription", "comment", "remarks", "detailComment"),
    )

    images_val = (
        blob.get("images")
        or blob.get("photos")
        or blob.get("gallery")
        or blob.get("media")
        or blob.get("imageList")
    )
    images = tuple(dict.fromkeys(_collect_images(images_val)))

    return {
        "brand": brand,
        "model": model,
        "trim": trim,
        "year": year_int,
        "mileage": mileage_int,
        "color": color,
        "transmission": transmission,
        "price": price_val,
        "description": description,
        "images": images,
    }


def _extract_from_legacy_initial_state(html: str, listing_url: str) -> BamaDetailExtract:
    state = extract_initial_state(html)
    blob = _best_listing_blob(state)
    fields = _extract_fields(blob)

    from_url = _extract_external_id(listing_url, fallback="").strip()
    from_blob = _listing_id_from_blob(blob)
    external_id = (from_blob or from_url or "unknown").strip() or "unknown"

    blob_keys_sample = sorted(blob.keys())[:40]
    LOGGER.info(
        "bama_detail_parser parse_bama_listing_from_html url=%s external_id=%r "
        "(from_blob=%r from_url=%r) price=%s brand=%r model=%r mileage=%s "
        "blob_top_keys=%s images_count=%s",
        listing_url,
        external_id,
        from_blob,
        from_url or None,
        fields.get("price"),
        fields.get("brand"),
        fields.get("model"),
        fields.get("mileage"),
        blob_keys_sample,
        len(fields.get("images") or ()),
    )
    if external_id == "unknown":
        LOGGER.warning(
            "bama_detail_parser: external_id still 'unknown' — URL segment and blob id keys missing. url=%s",
            listing_url,
        )

    return BamaDetailExtract(
        external_id=external_id,
        brand=fields["brand"],
        model=fields["model"],
        trim=fields["trim"],
        year=fields["year"],
        mileage=fields["mileage"],
        color=fields["color"],
        transmission=fields["transmission"],
        price=fields["price"],
        description=fields["description"],
        images=tuple(fields["images"]),
        listing_url=listing_url,
        price_debug_note="__INITIAL_STATE__",
    )


def _merge_nuxt_regex_into_extract(
    html: str,
    base: BamaDetailExtract,
    nuxt_inner: dict[str, Any] | None,
    *,
    price_source: str,
) -> BamaDetailExtract:
    nf = _extract_fields_from_nuxt_inner(nuxt_inner) if nuxt_inner else None

    ld_irr: int | None = None
    if price_source == "ld+json" and base.price is not None:
        try:
            ld_irr = int(base.price)
        except (ValueError, TypeError, OverflowError):
            ld_irr = None

    nuxt_irr = nf["price_irr"] if nf else None
    nuxt_neg = bool(nf["negotiable"]) if nf else False

    regex_irr, regex_tag = _regex_price_irr_from_html(html)
    if nuxt_neg:
        regex_irr, regex_tag = None, ""

    chosen_irr = nuxt_irr or ld_irr or regex_irr

    merged_notes: list[str] = []
    if base.price_debug_note:
        merged_notes.append(base.price_debug_note)
    merged_notes.append(price_source)
    if nf and nf.get("price_note"):
        merged_notes.append(str(nf["price_note"]))
    if chosen_irr and ld_irr and chosen_irr == ld_irr:
        merged_notes.append("chosen_ld+json_irr")
    if chosen_irr and nuxt_irr and chosen_irr == nuxt_irr:
        merged_notes.append("chosen_nuxt_irr")
    if chosen_irr and regex_irr and chosen_irr == regex_irr:
        merged_notes.append(regex_tag or "chosen_regex_irr")

    negotiable_final = False
    if chosen_irr:
        negotiable_final = False
    else:
        negotiable_final = nuxt_neg or _html_marked_negotiable(html)

    if chosen_irr:
        price_out = irr_integer_to_decimal(chosen_irr)
    elif price_source == "initial" and base.price is not None:
        price_out = base.price
    else:
        price_out = None

    note_str = "; ".join(merged_notes)

    brand = nf["brand"] if nf and nf["brand"] else base.brand
    model = nf["model"] if nf and nf["model"] else base.model
    trim = nf["trim"] if nf and nf["trim"] else base.trim
    year = None
    year_sources: list[str] = []

    if nf and nf["year"] is not None:
        candidate = _normalize_model_year(nf["year"])
        if candidate is not None:
            year = candidate
            year_sources.append("nuxt.vehicle.year")

    if year is None and base.year is not None:
        candidate = _normalize_model_year(base.year)
        if candidate is not None:
            year = candidate
            year_sources.append("base.year")

    if year is None:
        candidate = _extract_model_year_from_url_tail(base.listing_url)
        if candidate is not None:
            year = candidate
            year_sources.append("url")

    if year is None:
        candidate = _extract_model_year_candidates_from_text(base.model)
        if candidate is not None:
            year = candidate
            year_sources.append("model")

    if year is None:
        candidate = _extract_model_year_candidates_from_text(base.trim)
        if candidate is not None:
            year = candidate
            year_sources.append("trim")

    if year is None:
        candidate = _extract_model_year_candidates_from_text(base.description)
        if candidate is not None:
            year = candidate
            year_sources.append("description")

    if year is None:
        LOGGER.warning(
            "bama_detail_parser model_year unresolved url=%s model=%r trim=%r description_head=%r",
            base.listing_url,
            base.model,
            base.trim,
            (base.description or "")[:180],
        )
    elif year_sources:
        LOGGER.info(
            "bama_detail_parser model_year resolved=%s source=%s url=%s",
            year,
            ",".join(year_sources),
            base.listing_url,
        )
    mileage = nf["mileage"] if nf and nf["mileage"] is not None else base.mileage
    color = nf["color"] if nf and nf["color"] else base.color
    transmission = nf["gearbox_text"] if nf and nf["gearbox_text"] else base.transmission
    city = nf["city"] if nf and nf["city"] else base.city

    return replace(
        base,
        brand=brand,
        model=model,
        trim=trim,
        year=year,
        mileage=mileage,
        color=color,
        transmission=transmission,
        city=city,
        price=price_out,
        price_is_negotiable=negotiable_final,
        price_debug_note=note_str,
    )


def parse_bama_listing_from_html(html: str, listing_url: str) -> BamaDetailExtract:
    nuxt_inner = _try_get_nuxt_pdp_inner(html, listing_url)
    created_at_source = _extract_created_at_source_from_hidden_data(html, listing_url)

    base: BamaDetailExtract | None = None
    price_source = ""
    try:
        base = _extract_from_legacy_initial_state(html, listing_url)
        price_source = "initial"
    except ValueError as initial_exc:
        LOGGER.info(
            "bama_detail_parser: __INITIAL_STATE__ missing (%s); trying schema.org ld+json.",
            initial_exc,
        )
        base = _extract_from_schema_org_ld_json(html, listing_url)
        price_source = "ld+json"

    if base is None:
        ext = (_extract_external_id(listing_url, fallback="").strip() or "unknown")
        LOGGER.warning(
            "bama_detail_parser: falling back to URL-only shell (no ld+json Product). url=%s",
            listing_url,
        )
        base = BamaDetailExtract(
            external_id=ext,
            brand=None,
            model=None,
            trim=None,
            year=None,
            mileage=None,
            color=None,
            transmission=None,
            price=None,
            description=None,
            images=tuple(),
            listing_url=listing_url,
            price_debug_note="empty_shell",
        )
        price_source = "empty"

    merged = _merge_nuxt_regex_into_extract(html, base, nuxt_inner, price_source=price_source)
    merged = replace(merged, created_at_source=created_at_source)

    body_condition: str | None = None
    body_condition_source_path: str | None = None
    if nuxt_inner:
        body_condition, body_condition_source_path = _body_status_from_nuxt_inner(nuxt_inner)
    if body_condition is None:
        body_condition, body_condition_source_path = _body_status_from_visible_html(html)
    merged = replace(
        merged,
        body_condition=body_condition,
        body_condition_source_path=body_condition_source_path,
    )

    LOGGER.info(
        "bama_detail_parser merged external_id=%r price=%s negotiable=%s city=%r mileage=%s "
        "body_condition=%r body_path=%r note=%r",
        merged.external_id,
        merged.price,
        merged.price_is_negotiable,
        merged.city,
        merged.mileage,
        merged.body_condition,
        merged.body_condition_source_path,
        merged.price_debug_note,
    )

    if merged.external_id == "unknown":
        LOGGER.warning(
            "bama_detail_parser merged extract still unknown external_id listing_url=%s",
            listing_url,
        )

    return merged


def fetch_and_parse_bama_listing(url: str) -> BamaDetailExtract:
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{BAMA_DOMAIN}/{normalized.lstrip('/')}"
    LOGGER.info("bama_detail_parser fetch_and_parse_bama_listing fetching url=%s", normalized)
    html = fetch_listing_html(normalized)
    LOGGER.info(
        "bama_detail_parser fetched HTML length=%s chars url=%s",
        len(html),
        normalized,
    )
    extract = parse_bama_listing_from_html(html, normalized)
    LOGGER.info(
        "bama_detail_parser fetch complete external_id=%r price=%s listing_url=%s",
        extract.external_id,
        extract.price,
        extract.listing_url,
    )
    return extract
