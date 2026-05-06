"""Fetch and parse one Hamrah-e-Mechanic listing detail page."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from app.scrapers.bama_scraper import USER_AGENTS, _extract_external_id

LOGGER = logging.getLogger(__name__)

HM_DOMAIN = "https://www.hamrah-mechanic.com"
HM_HOST = "www.hamrah-mechanic.com"
HM_DIRECT_IP = (os.getenv("HM_DIRECT_IP", "").strip() or "185.143.232.253")
HM_DIRECT_IP_CANDIDATES = tuple(
    ip.strip()
    for ip in (
        HM_DIRECT_IP,
        os.getenv("HM_DIRECT_IP_2", "").strip() or "185.143.235.253",
        os.getenv("HM_DIRECT_IP_3", "").strip() or "185.143.232.201",
        os.getenv("HM_DIRECT_IP_4", "").strip() or "185.143.235.201",
    )
    if ip.strip()
)
_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

_TIMESTAMP_KEYS_EXACT = frozenset(
    {
        "published_at",
        "publishedat",
        "publish_date",
        "publishdate",
        "created_at",
        "createdat",
        "updated_at",
        "updatedat",
        "date_published",
        "datepublished",
        "post_date",
        "publish_time",
    }
)


@dataclass(frozen=True)
class HMDetailExtract:
    external_id: str
    brand: str | None
    model: str | None
    trim: str | None
    year: int | None
    mileage: int | None
    body_condition: str | None
    city: str | None
    price: Decimal | None
    description: str | None
    images: tuple[str, ...]
    listing_url: str
    created_at_source: str | None = None
    price_debug_note: str = ""


class HMParseIntegrityError(RuntimeError):
    """Raised when HM payload misses required listing integrity fields."""


def _normalize_digit_chars(text: str) -> str:
    t = text.translate(_FA_AR_TO_EN_DIGITS).strip()
    for ch in ("،", ",", "٬", " ", "\u200c", "\u200f", "\u202b", "\u202c"):
        t = t.replace(ch, "")
    return t


def _extract_int(value: Any) -> int | None:
    if value is None:
        return None
    normalized = _normalize_digit_chars(str(value))
    digits = re.sub(r"[^\d]", "", normalized)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _looks_like_persian_relative_time(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    hints = ("پیش", "لحظاتی", "دقایقی", "ساعتی", "امروز", "دیروز", " دقیقه", " ساعت", " روز")
    return any(h in lowered for h in hints)


def _parse_persian_relative_time_to_iso(text: str, *, now: datetime | None = None) -> str | None:
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
        return (current - timedelta(minutes=int(minute_match.group(1)))).isoformat()

    hour_match = re.search(r"(\d{1,3})\s*ساعت(?:ی)?\s*پیش", normalized)
    if hour_match:
        return (current - timedelta(hours=int(hour_match.group(1)))).isoformat()

    day_match = re.search(r"(\d{1,3})\s*روز\s*پیش", normalized)
    if day_match:
        return (current - timedelta(days=int(day_match.group(1)))).isoformat()

    today_time = re.search(r"امروز\s*(\d{1,2}):(\d{1,2})", normalized)
    if today_time:
        hh, mm = int(today_time.group(1)), int(today_time.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return current.replace(hour=hh, minute=mm, second=0).isoformat()

    yday_time = re.search(r"دیروز\s*(\d{1,2}):(\d{1,2})", normalized)
    if yday_time:
        hh, mm = int(yday_time.group(1)), int(yday_time.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return (current - timedelta(days=1)).replace(hour=hh, minute=mm, second=0).isoformat()

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
        seconds = numeric / 1000 if numeric > 1_000_000_000_000 else numeric
        min_epoch = 946684800
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
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.astimezone().replace(microsecond=0).isoformat()
    except ValueError:
        return None


def _find_timestamp_candidate(node: Any) -> tuple[str | None, str]:
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
        elif any(k in key_norm for k in ("publish", "published", "create", "created", "update", "date", "time")):
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


def _first_non_empty_string(values: list[Any]) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _collect_images(node: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(node, str):
        if node.startswith(("http://", "https://")):
            urls.append(node)
        return urls
    if isinstance(node, list):
        for item in node:
            urls.extend(_collect_images(item))
        return urls
    if isinstance(node, dict):
        for key in ("url", "src", "image", "imageUrl", "largeUrl"):
            urls.extend(_collect_images(node.get(key)))
        for key in ("images", "gallery", "photos", "items"):
            urls.extend(_collect_images(node.get(key)))
    return urls


def _iter_dict_nodes(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dict_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dict_nodes(item)


def _extract_labeled_value(node: Any, labels: tuple[str, ...]) -> str | None:
    label_set = {lbl.strip() for lbl in labels if lbl and lbl.strip()}
    if not label_set:
        return None

    for item in _iter_dict_nodes(node):
        named = _first_non_empty_string(
            [
                item.get("label"),
                item.get("title"),
                item.get("name"),
                item.get("key"),
                item.get("specName"),
                item.get("specTitle"),
            ]
        )
        if not named:
            continue
        if named.strip() not in label_set:
            continue
        value = _first_non_empty_string(
            [
                item.get("value"),
                item.get("text"),
                item.get("titleValue"),
                item.get("displayValue"),
                item.get("nameValue"),
                item.get("answer"),
            ]
        )
        if value:
            return value
    return None


def _extract_next_data_brand_model(next_data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    brand: str | None = None
    model: str | None = None
    trim: str | None = None

    for node in _iter_dict_nodes(next_data):
        if brand is None:
            brand = _first_non_empty_string(
                [
                    node.get("brandName"),
                    node.get("carBrandName"),
                    node.get("brand"),
                    node.get("carBrandEnglishName"),
                ]
            )
        if model is None:
            model = _first_non_empty_string(
                [
                    node.get("modelName"),
                    node.get("carModelName"),
                    node.get("model"),
                    node.get("carModelEnglishName"),
                ]
            )
        if trim is None:
            trim = _first_non_empty_string(
                [
                    node.get("trimName"),
                    node.get("trim"),
                    node.get("typeName"),
                    node.get("carTypeName"),
                ]
            )
        if brand and model:
            break
    return brand, model, trim


def _extract_next_data_mileage(next_data: dict[str, Any]) -> int | None:
    mileage_labels = ("کارکرد", "کارکرد خودرو", "کیلومتر")
    labeled = _extract_labeled_value(next_data, mileage_labels)
    parsed = _extract_int(labeled) if labeled else None
    if parsed is not None:
        return parsed

    for node in _iter_dict_nodes(next_data):
        for key in ("mileage", "km", "kilometer", "kilometers", "carMileage"):
            if key in node:
                value = _extract_int(node.get(key))
                if value is not None:
                    return value
    return None


def _extract_next_data_body_condition(next_data: dict[str, Any]) -> str | None:
    body = _extract_labeled_value(next_data, ("وضعیت بدنه",))
    if body:
        return body

    for node in _iter_dict_nodes(next_data):
        for key in ("bodyCondition", "body_condition", "bodyStatus", "body_status"):
            raw = node.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _extract_next_data(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None
    raw = script.string or script.get_text()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_created_at_source(html: str, next_data: dict[str, Any] | None, listing_url: str) -> str | None:
    if next_data:
        iso, source = _find_timestamp_candidate(next_data)
        if iso:
            LOGGER.info("hm_scraper created_at from __NEXT_DATA__ path=%s", source)
            return iso

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    for pattern in (
        r"زمان انتشار\s*[:：]?\s*(لحظاتی پیش|دقایقی پیش|ساعتی پیش|چند دقیقه پیش)",
        r"زمان انتشار\s*[:：]?\s*([\d۰-۹٠-٩]{1,3}\s*(?:دقیقه|ساعت|روز)(?:\s*ای)?\s*پیش)",
        r"زمان انتشار\s*[:：]?\s*((?:امروز|دیروز)(?:\s*(?:ساعت)?\s*[\d۰-۹٠-٩]{1,2}:[\d۰-۹٠-٩]{1,2})?)",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        iso = _to_iso8601_timestamp(match.group(1))
        if iso:
            LOGGER.info("hm_scraper created_at from label fallback value=%r", match.group(1))
            return iso

    generic_match = re.search(
        r"(لحظاتی پیش|دقایقی پیش|ساعتی پیش|چند دقیقه پیش|[\d۰-۹٠-٩]{1,3}\s*(?:دقیقه|ساعت|روز)(?:\s*ای)?\s*پیش|(?:امروز|دیروز)(?:\s*(?:ساعت)?\s*[\d۰-۹٠-٩]{1,2}:[\d۰-۹٠-٩]{1,2})?)",
        text,
    )
    if generic_match:
        iso = _to_iso8601_timestamp(generic_match.group(1))
        if iso:
            LOGGER.info("hm_scraper created_at from generic text fallback value=%r", generic_match.group(1))
            return iso

    LOGGER.debug("hm_scraper created_at not found in page data url=%s", listing_url)
    return None


def _extract_price_decimal(next_data: dict[str, Any] | None, text: str) -> tuple[Decimal | None, str]:
    if next_data:
        priority_keys = (
            "expertPrice",
            "expert_price",
            "carExpertPrice",
            "verifiedPrice",
            "finalPrice",
            "offerPrice",
            "price",
        )
        for node in _iter_dict_nodes(next_data):
            for key in priority_keys:
                if key not in node:
                    continue
                number = _extract_int(node.get(key))
                if number and number > 0:
                    return Decimal(number), f"next_data:{key}"

        expert_labeled = _extract_labeled_value(next_data, ("قیمت کارشناسی", "قیمت", "قیمت نهایی"))
        expert_number = _extract_int(expert_labeled) if expert_labeled else None
        if expert_number and expert_number > 0:
            return Decimal(expert_number), "next_data:expert_labeled"

    price_match = re.search(r"([\d۰-۹٠-٩،٬,]{5,22})\s*(?:تومان|ریال)", text)
    if price_match:
        parsed = _extract_int(price_match.group(1))
        if parsed and parsed > 0:
            return Decimal(parsed), "html_text_regex"

    return None, ""


def _extract_hm_fields(html: str, listing_url: str, next_data: dict[str, Any] | None) -> HMDetailExtract:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    brand = None
    model = None
    year = None
    mileage = None
    city = None
    body_condition = None
    description = None
    trim = None
    images: tuple[str, ...] = tuple()

    if next_data:
        nd_brand, nd_model, nd_trim = _extract_next_data_brand_model(next_data)
        brand = nd_brand or brand
        model = nd_model or model
        trim = nd_trim or trim
        mileage = _extract_next_data_mileage(next_data) or mileage
        body_condition = _extract_next_data_body_condition(next_data) or body_condition

        for node in _iter_dict_nodes(next_data):
            if year is None:
                year = _extract_int(_first_non_empty_string([node.get("year"), node.get("modelYear"), node.get("carYear")]))
            if city is None:
                city = _first_non_empty_string(
                    [node.get("city"), node.get("cityName"), node.get("cityNamePersian"), node.get("location"), node.get("region")]
                )
            if description is None:
                description = _first_non_empty_string([node.get("description"), node.get("desc"), node.get("comment")])
            if year is not None and city and description:
                break

        images = tuple(dict.fromkeys(_collect_images(next_data)))

    if body_condition is None:
        body_match = re.search(r"وضعیت بدنه\s*[:：]?\s*([^\n\r|]+)", text)
        if body_match:
            body_condition = body_match.group(1).strip()

    if year is None:
        year_match = re.search(r"(13\d{2}|14\d{2})", text)
        if year_match:
            year = int(year_match.group(1))
    if mileage is None:
        m_match = re.search(r"([\d۰-۹٠-٩،٬,]+)\s*کیلومتر", text)
        if m_match:
            mileage = _extract_int(m_match.group(1))

    price, price_note = _extract_price_decimal(next_data, text)
    created_at_source = _extract_created_at_source(html, next_data, listing_url)
    external_id = _extract_external_id(listing_url, fallback="").strip() or "unknown"

    return HMDetailExtract(
        external_id=external_id,
        brand=brand,
        model=model,
        trim=trim,
        year=year,
        mileage=mileage,
        body_condition=body_condition,
        city=city,
        price=price,
        description=description,
        images=images,
        listing_url=listing_url,
        created_at_source=created_at_source,
        price_debug_note=price_note,
    )


def _dynamic_referer(listing_url: str) -> str:
    parsed = urlsplit(listing_url)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else HM_DOMAIN
    candidates = [
        base + "/cars-for-sale/",
        base + "/cars-for-sale/irankhodro/",
        base + "/cars-for-sale/irankhodro/denaplus/",
        base + "/",
    ]
    return random.choice(candidates)


def _random_headers(listing_url: str) -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": _dynamic_referer(listing_url),
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if HM_DIRECT_IP:
        headers["Host"] = HM_HOST
    return headers


def _build_direct_ip_url(url: str, ip: str | None = None, scheme: str | None = None) -> str:
    parts = urlsplit(url)
    target_ip = ip or HM_DIRECT_IP
    if not target_ip or not parts.netloc:
        return url
    if HM_HOST not in parts.netloc and "hamrah-mechanic.com" not in parts.netloc:
        return url
    out_scheme = scheme or parts.scheme or "https"
    return urlunsplit((out_scheme, target_ip, parts.path, parts.query, parts.fragment))


def fetch_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{HM_DOMAIN}/{normalized.lstrip('/')}"
    headers = _random_headers(normalized)
    attempts: list[tuple[str, bool]] = []
    if HM_DIRECT_IP_CANDIDATES:
        for ip in HM_DIRECT_IP_CANDIDATES:
            attempts.append((_build_direct_ip_url(normalized, ip=ip, scheme="https"), False))
            attempts.append((_build_direct_ip_url(normalized, ip=ip, scheme="http"), True))
    else:
        attempts.append((normalized, True))

    last_error: Exception | None = None
    for request_url, verify_ssl in attempts:
        try:
            time.sleep(random.uniform(3, 7))
            if not verify_ssl:
                requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
            response = requests.get(
                request_url,
                headers=headers,
                timeout=timeout_seconds,
                verify=verify_ssl,
                allow_redirects=True,
            )
            response.raise_for_status()
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning("hm_scraper request attempt failed url=%s detail=%s", request_url, exc)
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("hm_scraper request attempts exhausted without response")


def parse_hm_listing_from_html(html: str, listing_url: str) -> HMDetailExtract:
    next_data = _extract_next_data(html)
    extract = _extract_hm_fields(html, listing_url, next_data)

    if not extract.brand or not extract.model:
        raise HMParseIntegrityError(
            f"HM parse integrity failed (missing brandName/modelName) external_id={extract.external_id!r} url={listing_url}"
        )
    if extract.price is None:
        raise HMParseIntegrityError(
            f"HM parse integrity failed (missing numeric price) external_id={extract.external_id!r} url={listing_url}"
        )
    if extract.mileage is None or extract.mileage < 0:
        raise HMParseIntegrityError(
            f"HM parse integrity failed (missing/invalid mileage) external_id={extract.external_id!r} url={listing_url}"
        )

    return extract


def fetch_and_parse_hm_listing(url: str) -> HMDetailExtract:
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{HM_DOMAIN}/{normalized.lstrip('/')}"
    LOGGER.info("hm_scraper fetching listing url=%s", normalized)
    html = fetch_listing_html(normalized)
    extract = parse_hm_listing_from_html(html, normalized)
    LOGGER.info(
        "hm_scraper parsed external_id=%r brand=%r model=%r year=%s mileage=%s body_condition=%r price=%s",
        extract.external_id,
        extract.brand,
        extract.model,
        extract.year,
        extract.mileage,
        extract.body_condition,
        extract.price,
    )
    return extract
