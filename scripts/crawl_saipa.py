#!/usr/bin/env python3
"""
Crawler: Saipa Group (گروه سایپا) authorized internal dealership network.

Public entry URL:
  https://www.saipacorp.com/fa/agencies/internaldealers

Dealer data is served from an iframe-hosted ASP.NET WebForms app:
  https://dealer.saipayadak.org/reports/RptAgents.aspx

Working POST pattern (live browser trace, May 2026):

  1. GET RptAgents.aspx → extract session-bound __VIEWSTATE / __EVENTVALIDATION.
  2. Province hydrate (optional): full-form POST with
         __EVENTTARGET=ddl_State, ddl_State={id}, ddl_Car=B90
  3. Search: full-form POST with
         __EVENTTARGET= (empty), __EVENTARGUMENT= (empty),
         ddl_Car=B90, ddl_State={id}, BtnSearch=جستجو
     (URL-encoded: BtnSearch=%D8%AC%D8%B3%D8%AA%D8%AC%D9%88)

Usage:
  python scripts/crawl_saipa.py
  python scripts/crawl_saipa.py --output data/raw_saipa_dealers.json -v
  python scripts/crawl_saipa.py --province 22081 --delay 2.0
  python scripts/crawl_saipa.py --province تهران --format table

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests import Response, Session
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("saipa-dealers-crawler")

SAIPA_PUBLIC_URL = "https://www.saipacorp.com/fa/agencies/internaldealers"
DEALER_BASE_URL = "https://dealer.saipayadak.org"
REPORTS_URL = f"{DEALER_BASE_URL}/reports/RptAgents.aspx"
DEFAULT_OUTPUT = Path("data/raw_saipa_dealers.json")

FIELD_CAR = "ddl_Car"
FIELD_STATE = "ddl_State"
FIELD_CITY = "ddl_City"
FIELD_SEARCH = "BtnSearch"
FIELD_OPTION = "chkOption"

DEFAULT_CAR_CODE = "B90"
SEARCH_BUTTON_VALUE = "جستجو"

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_DELAY_S = 3.0
BLOCKED_HTTP_CODES = frozenset({403, 429})

SCRIPT_MANAGER = "ScriptManager1"
SEARCH_PANEL_ID = "u1"
RESULTS_PANEL_ID = "tu1"
SEARCH_ASYNC_TRIGGER = f"{SEARCH_PANEL_ID}|BtnSearch"

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
]

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
PHONE_RE = re.compile(
    r"(?:0\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}|0\d{10}|09\d{9})"
)
POSTBACK_RE = re.compile(
    r"__doPostBack\('([^']+)'(?:,'([^']*)')?\)",
    re.IGNORECASE,
)

ASP_NET_HIDDEN_FIELDS = (
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__LASTFOCUS",
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION",
)


@dataclass
class SearchFilters:
    province_id: str
    city_id: str | None = None
    car_code: str = DEFAULT_CAR_CODE
    option_dealers_only: bool = False


@dataclass
class DealershipRecord:
    dealer_code: str | None
    name: str | None
    manager: str | None
    province: str | None
    city: str | None
    address: str | None
    phone_numbers: list[str] = field(default_factory=list)
    agency_type: str | None = None
    status: str | None = None
    warranty_code: str | None = None
    search_province_id: str | None = None
    search_city_id: str | None = None
    source_page: int | None = None
    source: str = "saipacorp.com"
    source_url: str = SAIPA_PUBLIC_URL
    reports_url: str = REPORTS_URL
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    raw_row: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ProvinceSearchResult:
    html: str
    payload: dict[str, str]
    stopped_gracefully: bool = False
    stop_reason: str | None = None
    access_blocked: bool = False


class SaipaAccessBlockedError(RuntimeError):
    """Raised when saipayadak.org returns 403/429 (WAF or rate limit)."""


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def clean_farsi_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(value)
    text = text.replace("\xa0", " ").replace("\u200c", " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def normalize_dealer_code(value: str | None) -> str | None:
    text = clean_farsi_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits or text


def extract_phone_numbers(*values: str | None) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for match in PHONE_RE.findall(value):
            normalized = re.sub(r"\D", "", match)
            if len(normalized) >= 10 and normalized not in seen:
                seen.add(normalized)
                phones.append(normalized)
    return phones


def is_aspnet_server_error(raw_text: str) -> bool:
    return raw_text.startswith("0|error|") or "error.htm" in raw_text.lower()


def parse_aspnet_delta_response(raw_text: str) -> tuple[dict[str, str], dict[str, str], str | None]:
    """Return (panels, hidden_fields, page_redirect)."""
    panels: dict[str, str] = {}
    hidden_fields: dict[str, str] = {}
    page_redirect: str | None = None

    if not raw_text.startswith("1|"):
        return panels, hidden_fields, page_redirect

    parts = raw_text.split("|")
    index = 0
    while index < len(parts):
        token = parts[index]
        if token == "updatePanel" and index + 2 < len(parts):
            panels[parts[index + 1]] = parts[index + 2]
            index += 3
            continue
        if token == "hiddenField" and index + 2 < len(parts):
            hidden_fields[parts[index + 1]] = parts[index + 2]
            index += 3
            continue
        if token == "pageRedirect" and index + 2 < len(parts):
            page_redirect = parts[index + 2]
            index += 3
            continue
        index += 1

    return panels, hidden_fields, page_redirect


def apply_delta_hidden_fields(payload: dict[str, str], hidden_fields: dict[str, str]) -> dict[str, str]:
    refreshed = dict(payload)
    refreshed.update(hidden_fields)
    return refreshed


def is_runtime_error_page(html: str) -> bool:
    lowered = html.lower()
    return "runtime error" in lowered or "<title>error</title>" in lowered


def build_form_payload(
    soup: BeautifulSoup,
    *,
    include_search_button: bool = False,
) -> dict[str, str]:
    """Collect all ASP.NET form fields from the latest GET/POST HTML."""
    form = soup.find("form", id="form1")
    if form is None:
        raise RuntimeError("form1 not found in RptAgents HTML")

    payload: dict[str, str] = {}
    for element in form.find_all("input"):
        name = element.get("name")
        if not name:
            continue
        input_type = (element.get("type") or "text").lower()
        if input_type == "submit":
            if include_search_button and name == FIELD_SEARCH:
                payload[name] = SEARCH_BUTTON_VALUE
            continue
        if input_type in ("checkbox", "radio"):
            if element.get("checked"):
                payload[name] = element.get("value") or "on"
        else:
            payload[name] = element.get("value") or ""

    for element in form.find_all("select"):
        name = element.get("name")
        if not name:
            continue
        selected = element.find("option", selected=True)
        if selected is not None:
            payload[name] = selected.get("value") or ""
        else:
            payload[name] = ""

    return payload


def build_state_post_payload(
    base_payload: dict[str, str],
    filters: SearchFilters,
) -> dict[str, str]:
    payload = dict(base_payload)
    payload["__EVENTTARGET"] = FIELD_STATE
    payload["__EVENTARGUMENT"] = ""
    payload[FIELD_STATE] = filters.province_id
    payload[FIELD_CAR] = filters.car_code
    if filters.city_id is not None:
        payload[FIELD_CITY] = filters.city_id
    payload.pop(FIELD_SEARCH, None)
    return payload


def build_search_post_payload(
    base_payload: dict[str, str],
    filters: SearchFilters,
) -> dict[str, str]:
    payload = dict(base_payload)
    payload["__EVENTTARGET"] = ""
    payload["__EVENTARGUMENT"] = ""
    payload["__LASTFOCUS"] = ""
    payload[FIELD_STATE] = filters.province_id
    payload[FIELD_CAR] = filters.car_code
    payload[FIELD_CITY] = filters.city_id if filters.city_id is not None else payload.get(FIELD_CITY, "")
    if filters.option_dealers_only:
        payload[FIELD_OPTION] = "on"
    else:
        payload.pop(FIELD_OPTION, None)
    payload[FIELD_SEARCH] = SEARCH_BUTTON_VALUE
    return payload


def load_province_options(soup: BeautifulSoup) -> list[tuple[str, str]]:
    select = soup.find("select", {"id": FIELD_STATE}) or soup.find(
        "select", {"name": FIELD_STATE}
    )
    if select is None:
        return []

    provinces: list[tuple[str, str]] = []
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        label = clean_farsi_text(option.get_text(strip=True)) or ""
        if not value or not label or label.startswith("--"):
            continue
        provinces.append((value, label))
    return provinces


def resolve_province_filter(
    provinces: list[tuple[str, str]],
    province_arg: str | None,
) -> list[tuple[str, str]]:
    if not province_arg:
        return provinces

    needle = clean_farsi_text(province_arg) or province_arg.strip()
    if needle.isdigit():
        matched = [(value, label) for value, label in provinces if value == needle]
        if matched:
            return matched
        raise RuntimeError(f"Province id {needle!r} not found in ddl_State")

    lowered = needle.lower()
    exact = [(value, label) for value, label in provinces if label.lower() == lowered]
    if exact:
        return exact

    partial = [
        (value, label)
        for value, label in provinces
        if lowered in label.lower() or label.lower() in lowered
    ]
    if partial:
        return partial

    raise RuntimeError(f"Province filter {province_arg!r} did not match any ddl_State option")


def _header_index_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        normalized = clean_farsi_text(header) or ""
        if "کد" in normalized and (
            "تابلو" in normalized or "نمای" in normalized or "گاران" in normalized
        ):
            mapping["dealer_code"] = index
        elif "نام" in normalized and "نمای" in normalized:
            mapping["name"] = index
        elif "مدیر" in normalized or "مدیریت" in normalized:
            mapping["manager"] = index
        elif "استان" in normalized:
            mapping.setdefault("province", index)
        elif "شهر" in normalized:
            mapping.setdefault("city", index)
        elif "تلفن" in normalized:
            mapping.setdefault("phone", index)
        elif "آدرس" in normalized:
            mapping["address"] = index
        elif "نوع" in normalized or "رتبه" in normalized or "وضع" in normalized:
            mapping.setdefault("agency_type", index)
    return mapping


def _record_from_cells(
    cells: list[str],
    *,
    header_map: dict[str, int],
    search_province_id: str | None,
    search_city_id: str | None,
    source_page: int | None,
    raw_cells: list[str],
) -> DealershipRecord | None:
    def pick(key: str) -> str | None:
        index = header_map.get(key)
        if index is None or index < 0 or index >= len(cells):
            return None
        value = cells[index]
        return value or None

    dealer_code = normalize_dealer_code(pick("dealer_code"))
    if not dealer_code:
        return None

    phone_raw = pick("phone")
    agency_type = pick("agency_type")
    return DealershipRecord(
        dealer_code=dealer_code,
        name=pick("name"),
        manager=pick("manager"),
        province=pick("province"),
        city=pick("city"),
        address=pick("address"),
        phone_numbers=extract_phone_numbers(phone_raw),
        agency_type=agency_type,
        status=agency_type,
        search_province_id=search_province_id,
        search_city_id=search_city_id,
        source_page=source_page,
        raw_row={"cells": raw_cells, "header_map": header_map},
    )


def _results_scope(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    for heading in soup.find_all(["div", "span", "td", "th"], string=re.compile("نتايج جستجو")):
        panel = heading.find_parent("div", class_=re.compile("panel"))
        if panel is not None:
            return panel
    return soup


def parse_search_results_html(
    html: str,
    *,
    search_province_id: str | None = None,
    search_city_id: str | None = None,
    source_page: int = 1,
) -> list[DealershipRecord]:
    soup = BeautifulSoup(html, "html.parser")
    scope = _results_scope(soup)
    records: list[DealershipRecord] = []

    for table in scope.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [
            clean_farsi_text(cell.get_text(" ", strip=True)) or ""
            for cell in header_cells
        ]
        if not any(headers):
            continue

        header_map = _header_index_map(headers)
        if "dealer_code" not in header_map:
            continue

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            raw_cells = [
                clean_farsi_text(cell.get_text(" ", strip=True)) or ""
                for cell in cells
            ]
            if not any(raw_cells):
                continue
            record = _record_from_cells(
                raw_cells,
                header_map=header_map,
                search_province_id=search_province_id,
                search_city_id=search_city_id,
                source_page=source_page,
                raw_cells=raw_cells,
            )
            if record is not None:
                records.append(record)

    return records


def extract_pager_postbacks(html: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str, str]] = []
    for anchor in soup.find_all("a", href=True):
        match = POSTBACK_RE.search(anchor["href"])
        if not match:
            continue
        label = clean_farsi_text(anchor.get_text(strip=True)) or ""
        links.append((match.group(1), match.group(2) or "", label))
    return links


class SaipaSessionClient:
    """Session-based HTTP client for Saipa RptAgents.aspx."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        verify_ssl: bool = False,
        user_agent: str | None = None,
        warmup_public_page: bool = True,
    ) -> None:
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.warmup_public_page = warmup_public_page
        self.session: Session = requests.Session()
        self.session.verify = verify_ssl
        self._warmed_up = False

    def _get_headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if referer is None else "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _post_headers(self) -> dict[str, str]:
        return {
            **self._get_headers(referer=REPORTS_URL),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": DEALER_BASE_URL,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }

    @staticmethod
    def _decode_response(response: Response) -> str:
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        max_attempts: int = 3,
        **kwargs: Any,
    ) -> Response:
        last_error: RequestException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
            except (SSLError, Timeout, ReadTimeout, ConnectionError, RequestException) as exc:
                last_error = exc
                LOGGER.warning(
                    "HTTP %s %s failed (attempt %d/%d): %s",
                    method,
                    url,
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts:
                    time.sleep(min(2.0 * attempt, 6.0))
        assert last_error is not None
        raise last_error

    def _check_blocked_status(self, response: Response) -> None:
        if response.status_code in BLOCKED_HTTP_CODES:
            raise SaipaAccessBlockedError(
                f"Access blocked by server (HTTP {response.status_code})"
            )

    def fetch_reports_page(self, *, warmup: bool | None = None) -> tuple[str, dict[str, str], BeautifulSoup]:
        """
        GET RptAgents.aspx and return fresh ASP.NET tokens bound to the session.

        The saipacorp wrapper is fetched at most once per session to reduce rate-limit risk.
        """
        do_warmup = self.warmup_public_page if warmup is None else warmup
        if do_warmup and not self._warmed_up:
            self._request_with_retries(
                "GET",
                SAIPA_PUBLIC_URL,
                headers=self._get_headers(referer="https://www.saipacorp.com/"),
            )
            self._warmed_up = True

        response = self._request_with_retries(
            "GET",
            REPORTS_URL,
            headers=self._get_headers(referer=SAIPA_PUBLIC_URL),
        )
        self._check_blocked_status(response)
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to load reports page (HTTP {response.status_code})"
            )

        html = self._decode_response(response)
        if is_runtime_error_page(html):
            raise RuntimeError("GET RptAgents.aspx returned a runtime error page")

        soup = BeautifulSoup(html, "html.parser")
        payload = build_form_payload(soup)
        if not payload.get("__VIEWSTATE") or not payload.get("__EVENTVALIDATION"):
            raise RuntimeError("Missing __VIEWSTATE or __EVENTVALIDATION on reports page")

        LOGGER.debug(
            "Fetched fresh tokens (viewstate=%d bytes, validation=%d bytes, cookies=%d)",
            len(payload["__VIEWSTATE"]),
            len(payload["__EVENTVALIDATION"]),
            len(self.session.cookies),
        )
        return html, payload, soup

    def post_form(self, payload: dict[str, str]) -> str:
        response = self._request_with_retries(
            "POST",
            REPORTS_URL,
            data=payload,
            headers=self._post_headers(),
        )
        self._check_blocked_status(response)
        if response.status_code != 200:
            raise RuntimeError(f"POST RptAgents.aspx failed (HTTP {response.status_code})")

        html = self._decode_response(response)
        if is_runtime_error_page(html):
            raise RuntimeError("POST RptAgents.aspx returned a runtime error page")
        return html

    def _ajax_headers(self) -> dict[str, str]:
        return {
            **self._get_headers(referer=REPORTS_URL),
            "Accept": "*/*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": DEALER_BASE_URL,
            "Cache-Control": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def post_search_async(self, payload: dict[str, str]) -> tuple[str | None, dict[str, str]]:
        """
        BtnSearch is registered as an async postback control (ScriptManager1=u1|BtnSearch).
        Fallback when full-form POST returns HTTP 500 from some environments.
        """
        body = dict(payload)
        body[SCRIPT_MANAGER] = SEARCH_ASYNC_TRIGGER
        response = self._request_with_retries(
            "POST",
            REPORTS_URL,
            data=body,
            headers=self._ajax_headers(),
        )
        self._check_blocked_status(response)
        if response.status_code != 200:
            raise RuntimeError(
                f"Async search postback failed (HTTP {response.status_code})"
            )

        raw = self._decode_response(response)
        if is_aspnet_server_error(raw):
            raise RuntimeError(f"Async search delta error: {raw[:80]!r}")

        panels, hidden_fields, redirect = parse_aspnet_delta_response(raw)
        if redirect:
            raise RuntimeError(f"Async search redirected to {redirect!r}")

        results_html = panels.get(RESULTS_PANEL_ID)
        if not results_html:
            for panel_id, panel_html in panels.items():
                if RESULTS_PANEL_ID in panel_id:
                    results_html = panel_html
                    break

        refreshed = apply_delta_hidden_fields(payload, hidden_fields)
        return results_html, refreshed

    def hydrate_province(self, base_payload: dict[str, str], filters: SearchFilters) -> dict[str, str]:
        payload = build_state_post_payload(base_payload, filters)
        html = self.post_form(payload)
        soup = BeautifulSoup(html, "html.parser")
        refreshed = build_form_payload(soup)
        refreshed[FIELD_STATE] = filters.province_id
        refreshed[FIELD_CAR] = filters.car_code
        city_select = soup.find("select", {"id": FIELD_CITY})
        city_count = len(city_select.find_all("option")) if city_select else 0
        LOGGER.debug(
            "Province %s: ddl_State postback OK (%d city option(s))",
            filters.province_id,
            city_count,
        )
        return refreshed

    def search_province(
        self,
        filters: SearchFilters,
        *,
        hydrate_state: bool = False,
    ) -> ProvinceSearchResult:
        """
        Per-province flow:
          GET (fresh tokens, no wrapper re-warmup) → optional ddl_State post → BtnSearch post.
        """
        try:
            _, base_payload, _ = self.fetch_reports_page(warmup=False)
        except SaipaAccessBlockedError as exc:
            return ProvinceSearchResult(
                html="",
                payload={},
                stopped_gracefully=True,
                stop_reason=str(exc),
                access_blocked=True,
            )

        working_payload = dict(base_payload)
        working_payload[FIELD_STATE] = filters.province_id
        working_payload[FIELD_CAR] = filters.car_code

        if hydrate_state:
            try:
                working_payload = self.hydrate_province(base_payload, filters)
            except SaipaAccessBlockedError as exc:
                return ProvinceSearchResult(
                    html="",
                    payload=working_payload,
                    stopped_gracefully=True,
                    stop_reason=str(exc),
                    access_blocked=True,
                )
            except RuntimeError as exc:
                LOGGER.warning(
                    "Province %s: ddl_State postback failed (%s); searching with inline ddl_State",
                    filters.province_id,
                    exc,
                )

        search_payload = build_search_post_payload(working_payload, filters)
        LOGGER.debug(
            "Province %s search POST keys: %s",
            filters.province_id,
            sorted(search_payload.keys()),
        )

        html: str | None = None
        refreshed = working_payload
        try:
            html = self.post_form(search_payload)
            refreshed = build_form_payload(BeautifulSoup(html, "html.parser"))
        except SaipaAccessBlockedError as exc:
            return ProvinceSearchResult(
                html="",
                payload=working_payload,
                stopped_gracefully=True,
                stop_reason=str(exc),
                access_blocked=True,
            )
        except RuntimeError as full_post_error:
            LOGGER.warning(
                "Province %s: full-form search failed (%s); trying async BtnSearch",
                filters.province_id,
                full_post_error,
            )
            try:
                results_html, refreshed = self.post_search_async(search_payload)
                if not results_html:
                    raise RuntimeError("Async search returned empty results panel")
                html = results_html
            except SaipaAccessBlockedError as exc:
                return ProvinceSearchResult(
                    html="",
                    payload=working_payload,
                    stopped_gracefully=True,
                    stop_reason=str(exc),
                    access_blocked=True,
                )
            except RuntimeError as async_error:
                return ProvinceSearchResult(
                    html="",
                    payload=working_payload,
                    stopped_gracefully=True,
                    stop_reason=f"{full_post_error}; async fallback: {async_error}",
                )

        return ProvinceSearchResult(html=html or "", payload=refreshed)


class SaipaDealersCrawler:
    """Province-by-province Saipa internal dealer extraction."""

    def __init__(
        self,
        client: SaipaSessionClient,
        *,
        delay: float = DEFAULT_DELAY_S,
        car_code: str = DEFAULT_CAR_CODE,
        hydrate_state: bool = False,
    ) -> None:
        self.client = client
        self.delay = delay
        self.car_code = car_code
        self.hydrate_state = hydrate_state

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def _collect_paginated_records(
        self,
        initial_html: str,
        form_payload: dict[str, str],
        *,
        filters: SearchFilters,
        province_label: str,
    ) -> list[DealershipRecord]:
        records: list[DealershipRecord] = []
        page_number = 1
        current_html = initial_html
        payload = dict(form_payload)

        while True:
            page_records = parse_search_results_html(
                current_html,
                search_province_id=filters.province_id,
                search_city_id=filters.city_id,
                source_page=page_number,
            )
            for record in page_records:
                if not record.province:
                    record.province = province_label
            records.extend(page_records)
            LOGGER.info(
                "Province %s page %d: extracted %d dealer row(s)",
                filters.province_id,
                page_number,
                len(page_records),
            )

            next_target: tuple[str, str] | None = None
            for event_target, event_argument, label in extract_pager_postbacks(current_html):
                if label.isdigit() and int(label) == page_number + 1:
                    next_target = (event_target, event_argument)
                    break
            if next_target is None:
                for event_target, event_argument, label in extract_pager_postbacks(current_html):
                    if "بعد" in label:
                        next_target = (event_target, event_argument)
                        break
            if next_target is None:
                break

            event_target, event_argument = next_target
            page_payload = dict(payload)
            page_payload["__EVENTTARGET"] = event_target
            page_payload["__EVENTARGUMENT"] = event_argument
            page_payload.pop(FIELD_SEARCH, None)

            self._sleep()
            try:
                current_html = self.client.post_form(page_payload)
            except RuntimeError as exc:
                LOGGER.warning(
                    "Province %s pagination stopped on page %d: %s",
                    filters.province_id,
                    page_number + 1,
                    exc,
                )
                break

            payload = build_form_payload(BeautifulSoup(current_html, "html.parser"))
            page_number += 1

        return records

    def crawl(
        self,
        *,
        province_filter: str | None = None,
    ) -> list[DealershipRecord]:
        try:
            _, _, soup = self.client.fetch_reports_page(warmup=True)
        except SaipaAccessBlockedError as exc:
            LOGGER.warning("Cannot reach dealer portal (%s); saving empty result set", exc)
            return []

        provinces = resolve_province_filter(load_province_options(soup), province_filter)
        LOGGER.info("Crawling %d province(s) with ddl_Car=%s", len(provinces), self.car_code)

        all_records: list[DealershipRecord] = []
        for index, (province_id, province_label) in enumerate(provinces):
            if index > 0:
                self._sleep()

            LOGGER.info("Searching province %s (%s)", province_id, province_label)
            filters = SearchFilters(province_id=province_id, car_code=self.car_code)
            result = self.client.search_province(filters, hydrate_state=self.hydrate_state)

            if result.access_blocked:
                LOGGER.warning(
                    "Access blocked while searching province %s (%s): %s — "
                    "stopping crawl and saving %d record(s) collected so far",
                    province_id,
                    province_label,
                    result.stop_reason,
                    len(all_records),
                )
                break

            if result.stopped_gracefully:
                LOGGER.warning(
                    "Province %s (%s) skipped: %s",
                    province_id,
                    province_label,
                    result.stop_reason,
                )
                continue

            if not result.html.strip():
                LOGGER.info(
                    "Province %s (%s): empty results panel",
                    province_id,
                    province_label,
                )
                continue

            records = self._collect_paginated_records(
                result.html,
                result.payload,
                filters=filters,
                province_label=province_label,
            )
            all_records.extend(records)

        return self._dedupe_records(all_records)

    @staticmethod
    def _dedupe_records(records: list[DealershipRecord]) -> list[DealershipRecord]:
        seen: set[str] = set()
        unique: list[DealershipRecord] = []
        for record in records:
            key = record.dealer_code
            if not key:
                unique.append(record)
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(record)
        removed = len(records) - len(unique)
        if removed:
            LOGGER.info("Deduped %d duplicate dealer record(s) by dealer_code", removed)
        return unique


def write_json(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_table(records: list[DealershipRecord]) -> None:
    if not records:
        print("No dealerships extracted.")
        return

    headers = ("code", "name", "province", "city", "phones")
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.dealer_code or "-",
                (record.name or "-")[:34],
                (record.province or "-")[:14],
                (record.city or "-")[:16],
                ",".join(record.phone_numbers[:2]) or "-",
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt_row(list(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows[:40]:
        print(fmt_row(row))
    if len(rows) > 40:
        print(f"... and {len(rows) - 40} more")
    print()
    print(f"Total: {len(records)} dealerships")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Saipa Group internal dealerships (saipacorp.com / saipayadak.org)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Write JSON array to this path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--province",
        default=None,
        help="Limit crawl to one province (ddl_State value or Persian name)",
    )
    parser.add_argument(
        "--car",
        default=DEFAULT_CAR_CODE,
        help=f"ddl_Car value sent with every POST (default: {DEFAULT_CAR_CODE})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_S,
        help=f"Sleep between province searches in seconds (default: {DEFAULT_DELAY_S})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "both"),
        default="both",
        help="Console output format (default: both)",
    )
    parser.add_argument(
        "--hydrate-state",
        action="store_true",
        help="POST ddl_State before each search to load city dropdown (extra request)",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip one-time GET of saipacorp wrapper page at crawl start",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _configure_logging(args.verbose)

    client = SaipaSessionClient(
        timeout=args.timeout,
        warmup_public_page=not args.no_warmup,
    )
    crawler = SaipaDealersCrawler(
        client,
        delay=args.delay,
        car_code=args.car,
        hydrate_state=args.hydrate_state,
    )

    try:
        records = crawler.crawl(province_filter=args.province)
    except SaipaAccessBlockedError as exc:
        LOGGER.warning("Crawl stopped due to access block: %s", exc)
        records = []
    except (RequestException, RuntimeError) as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    LOGGER.info("Wrote %d records to %s", len(records), args.output.resolve())

    if args.format in ("table", "both"):
        print_table(records)

    if args.format in ("json", "both") and records:
        print(json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
