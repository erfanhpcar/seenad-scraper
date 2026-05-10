"""Fetch and parse one Karnameh listing detail page."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from app.scrapers.bama_scraper import _extract_external_id
from app.scrapers.hm_scraper import _classify_request_error, _to_iso8601_timestamp

LOGGER = logging.getLogger(__name__)

KN_DOMAIN = "https://karnameh.com"
KN_HOST = "karnameh.com"
KN_DIRECT_IP = (os.getenv("KN_DIRECT_IP", "").strip() or "185.166.104.4")
KN_DIRECT_IP_CANDIDATES = tuple(
    ip.strip()
    for ip in (
        KN_DIRECT_IP,
        os.getenv("KN_DIRECT_IP_2", "").strip() or "185.166.104.3",
        os.getenv("KN_DIRECT_IP_3", "").strip() or "185.166.104.4",
        os.getenv("KN_DIRECT_IP_4", "").strip() or "185.166.104.3",
    )
    if ip.strip()
)

KN_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
)

_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

_TIMESTAMP_HINT_KEYS = ("publish", "published", "create", "created", "update", "date", "time")
_BRAND_KEYS = ("brand", "brandName", "brand_name_fa", "brand_name_en", "make", "manufacturer")
_MODEL_KEYS = ("model", "modelName", "carModel", "brand_model", "model_name_fa", "model_name_en", "name")
_TRIM_KEYS = ("trim", "trimName", "variant", "version", "grade", "typeName")


@dataclass(frozen=True)
class KNDetailExtract:
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


class KNParseIntegrityError(RuntimeError):
    """Raised when Karnameh payload misses required listing integrity fields."""


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


def _first_non_empty_string(values: list[Any]) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _iter_dict_nodes(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dict_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dict_nodes(item)


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
        if not named or named.strip() not in label_set:
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


def _extract_brand_model_trim(next_data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    brand: str | None = None
    model: str | None = None
    trim: str | None = None

    for node in _iter_dict_nodes(next_data):
        node_keys = {str(k).lower() for k in node.keys()}
        car_context = any(
            key in node_keys
            for key in (
                "brand",
                "brandname",
                "brand_name_fa",
                "brand_name_en",
                "model",
                "modelname",
                "model_name_fa",
                "model_name_en",
                "brand_model",
                "title",
                "car_images",
                "advertisement_price",
            )
        )
        if brand is None:
            brand = _first_non_empty_string([node.get(k) for k in _BRAND_KEYS])
        if model is None:
            if car_context:
                model = _first_non_empty_string([node.get(k) for k in _MODEL_KEYS])
            else:
                model = _first_non_empty_string([node.get(k) for k in _MODEL_KEYS if k != "name"])
        if trim is None:
            trim = _first_non_empty_string([node.get(k) for k in _TRIM_KEYS])
        if brand and model:
            break
    return brand, model, trim


def _extract_price_decimal(next_data: dict[str, Any] | None, text: str) -> tuple[Decimal | None, str]:
    if next_data:
        priority_keys = (
            "expertPrice",
            "expert_price",
            "expertAppraisedPrice",
            "appraisedPrice",
            "carExpertPrice",
            "carExpertEstimatePrice",
            "verifiedPrice",
            "finalPrice",
            "price",
        )
        for node in _iter_dict_nodes(next_data):
            for key in priority_keys:
                if key not in node:
                    continue
                number = _extract_int(node.get(key))
                if number and number > 0:
                    return Decimal(number), f"next_data:{key}"

        expert_labeled = _extract_labeled_value(
            next_data,
            (
                "قیمت کارشناسی",
                "قیمت برآوردی",
                "قیمت کارشناسی کارنامه",
                "قیمت",
                "قیمت نهایی",
            ),
        )
        expert_number = _extract_int(expert_labeled) if expert_labeled else None
        if expert_number and expert_number > 0:
            return Decimal(expert_number), "next_data:expert_labeled"

    price_match = re.search(r"([\d۰-۹٠-٩،٬,]{5,22})\s*(?:تومان|ریال)", text)
    if price_match:
        parsed = _extract_int(price_match.group(1))
        if parsed and parsed > 0:
            return Decimal(parsed), "html_text_regex"

    return None, ""


def _extract_body_condition(next_data: dict[str, Any] | None, text: str) -> str | None:
    if next_data:
        chunks: list[str] = []
        lbl = _extract_labeled_value(
            next_data,
            (
                "وضعیت بدنه",
                "وضعیت رنگ",
                "رنگ شدگی",
                "خط و خش",
                "آسیب بدنه",
                "صافکاری",
            ),
        )
        if lbl:
            chunks.append(lbl)

        signal_tokens = ("body", "color", "paint", "scratch", "damage")
        for node in _iter_dict_nodes(next_data):
            for key in (
                "bodyCondition",
                "body_condition",
                "bodyStatus",
                "body_status",
                "colorStatus",
                "paintStatus",
                "scratchStatus",
                "damageStatus",
            ):
                raw = node.get(key)
                if isinstance(raw, str) and raw.strip():
                    chunks.append(raw.strip())
            # Keep only values that are attached to body/expert-report keys.
            for key, value in node.items():
                key_norm = str(key).strip().lower()
                if not any(token in key_norm for token in signal_tokens):
                    continue
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())

        unique = list(dict.fromkeys(c for c in chunks if c and c.strip()))
        if unique:
            return " | ".join(unique[:12])

    body_match = re.search(r"وضعیت بدنه\s*[:：]?\s*([^\n\r|]+)", text)
    if body_match:
        return body_match.group(1).strip()
    return None


def _extract_created_at_source(html: str, next_data: dict[str, Any] | None, listing_url: str) -> str | None:
    if next_data:
        for node in _iter_dict_nodes(next_data):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                key_norm = str(key).strip().lower()
                if not any(h in key_norm for h in _TIMESTAMP_HINT_KEYS):
                    continue
                iso = _to_iso8601_timestamp(value)
                if iso:
                    LOGGER.info("kn_scraper created_at from __NEXT_DATA__ key=%s", key)
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
            LOGGER.info("kn_scraper created_at from label fallback value=%r", match.group(1))
            return iso

    generic_match = re.search(
        r"(لحظاتی پیش|دقایقی پیش|ساعتی پیش|چند دقیقه پیش|[\d۰-۹٠-٩]{1,3}\s*(?:دقیقه|ساعت|روز)(?:\s*ای)?\s*پیش|(?:امروز|دیروز)(?:\s*(?:ساعت)?\s*[\d۰-۹٠-٩]{1,2}:[\d۰-۹٠-٩]{1,2})?)",
        text,
    )
    if generic_match:
        iso = _to_iso8601_timestamp(generic_match.group(1))
        if iso:
            LOGGER.info("kn_scraper created_at from generic text fallback value=%r", generic_match.group(1))
            return iso

    LOGGER.debug("kn_scraper created_at not found in page data url=%s", listing_url)
    return None


def _extract_kn_fields(html: str, listing_url: str, next_data: dict[str, Any] | None) -> KNDetailExtract:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    brand = None
    model = None
    trim = None
    year = None
    mileage = None
    city = None
    description = None
    images: tuple[str, ...] = tuple()

    if next_data:
        nd_brand, nd_model, nd_trim = _extract_brand_model_trim(next_data)
        brand = nd_brand or brand
        model = nd_model or model
        trim = nd_trim or trim

        mileage_labels = ("کارکرد", "کارکرد خودرو", "کیلومتر")
        labeled_mileage = _extract_labeled_value(next_data, mileage_labels)
        mileage = _extract_int(labeled_mileage) if labeled_mileage else mileage

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

    if year is None:
        year_match = re.search(r"(13\d{2}|14\d{2})", text)
        if year_match:
            year = int(year_match.group(1))
    if mileage is None:
        m_match = re.search(r"([\d۰-۹٠-٩،٬,]+)\s*کیلومتر", text)
        if m_match:
            mileage = _extract_int(m_match.group(1))

    body_condition = _extract_body_condition(next_data, text)
    price, price_note = _extract_price_decimal(next_data, text)
    created_at_source = _extract_created_at_source(html, next_data, listing_url)
    external_id = _extract_external_id(listing_url, fallback="").strip() or "unknown"

    return KNDetailExtract(
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
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else KN_DOMAIN
    candidates = [
        base + "/buy-used-car",
        base + "/sell-car",
        base + "/",
    ]
    return random.choice(candidates)


def _random_headers(listing_url: str, *, override_host: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(KN_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": _dynamic_referer(listing_url),
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if override_host:
        headers["Host"] = override_host
    return headers


def _build_direct_ip_url(url: str, ip: str | None = None, scheme: str | None = None) -> str:
    parts = urlsplit(url)
    target_ip = ip or KN_DIRECT_IP
    if not target_ip or not parts.netloc:
        return url
    if KN_HOST not in parts.netloc and "karnameh.com" not in parts.netloc:
        return url
    out_scheme = scheme or parts.scheme or "https"
    return urlunsplit((out_scheme, target_ip, parts.path, parts.query, parts.fragment))


def fetch_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    """Fetch one Karnameh listing detail page resiliently.

    Same attempt strategy as ``hm_scraper.fetch_listing_html``: try the real domain first
    (``/etc/hosts`` already maps it to the direct IP, so SNI works), then fall back to
    direct-IP HTTP, then direct-IP HTTPS with ``verify=False`` as a noisy last resort.
    """
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{KN_DOMAIN}/{normalized.lstrip('/')}"

    connect_timeout = float(os.getenv("KN_CONNECT_TIMEOUT_SECONDS", "20"))
    timeouts = (connect_timeout, timeout_seconds)
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

    attempts: list[tuple[str, bool, str | None, str]] = [
        (normalized, True, None, "domain_https"),
    ]
    for ip in KN_DIRECT_IP_CANDIDATES:
        attempts.append(
            (_build_direct_ip_url(normalized, ip=ip, scheme="http"), True, KN_HOST, f"direct_ip_http:{ip}")
        )
    for ip in KN_DIRECT_IP_CANDIDATES:
        attempts.append(
            (_build_direct_ip_url(normalized, ip=ip, scheme="https"), False, KN_HOST, f"direct_ip_https:{ip}")
        )

    last_error: Exception | None = None
    for request_url, verify_ssl, override_host, attempt_label in attempts:
        try:
            time.sleep(random.uniform(3, 7))
            headers = _random_headers(normalized, override_host=override_host)
            response = requests.get(
                request_url,
                headers=headers,
                timeout=timeouts,
                verify=verify_ssl,
                allow_redirects=True,
            )
            response.raise_for_status()
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            if attempt_label.startswith("direct_ip_https"):
                LOGGER.warning(
                    "kn_scraper succeeded on insecure direct-ip HTTPS fallback (verify=False) attempt=%s url=%s",
                    attempt_label,
                    url,
                )
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning(
                "kn_scraper request attempt failed attempt=%s url=%s reason=%s detail=%s",
                attempt_label,
                request_url,
                _classify_request_error(exc),
                exc,
            )
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("kn_scraper request attempts exhausted without response")


def parse_kn_listing_from_html(html: str, listing_url: str) -> KNDetailExtract:
    next_data = _extract_next_data(html)
    extract = _extract_kn_fields(html, listing_url, next_data)

    if not extract.brand or not extract.model:
        raise KNParseIntegrityError(
            f"KN parse integrity failed (missing brand/model) external_id={extract.external_id!r} url={listing_url}"
        )
    if extract.price is None:
        raise KNParseIntegrityError(
            f"KN parse integrity failed (missing numeric expert price) external_id={extract.external_id!r} url={listing_url}"
        )
    return extract


def fetch_and_parse_kn_listing(url: str) -> KNDetailExtract:
    normalized = url.strip()
    if not normalized.startswith("http"):
        normalized = f"{KN_DOMAIN}/{normalized.lstrip('/')}"
    LOGGER.info("kn_scraper fetching listing url=%s", normalized)
    html = fetch_listing_html(normalized)
    extract = parse_kn_listing_from_html(html, normalized)
    LOGGER.info(
        "kn_scraper parsed external_id=%r brand=%r model=%r trim=%r year=%s mileage=%s body_condition=%r price=%s",
        extract.external_id,
        extract.brand,
        extract.model,
        extract.trim,
        extract.year,
        extract.mileage,
        extract.body_condition,
        extract.price,
    )
    return extract
