#!/usr/bin/env python3
"""
Crawler: Iran Khodro (ایران‌خودرو / IKCO) authorized dealership network.

Target URL: https://www.ikco.ir/fa/Dealers.aspx

The page is ASP.NET WebForms with UpdatePanel partial postbacks and full-form
search submission. Observed behaviour (May 2026):

  - Initial GET returns empty SearchResult panel plus __VIEWSTATE tokens.
  - Province dropdown change triggers an AJAX delta postback (loads city list).
  - Search (btnSearch) must be a **full HTML POST** (not X-MicrosoftAjax delta);
    AJAX search redirects to /Error.htm.
  - Regional office filter (ddlRegion) is the most reliable nationwide driver.
  - Result rows live in AdvanceSearch1_SearchResult1_UpdatePanel1; pagination uses
    __doPostBack on DataPager controls via AJAX delta after the first search.
  - Phone numbers are not on the listing grid; fetch DealerDetail.aspx?id={code}.

Usage:
  python scripts/crawl_ikco.py
  python scripts/crawl_ikco.py --strategy region --output data/raw_ikco_dealers.json -v
  python scripts/crawl_ikco.py --strategy province --max-pages 2 --fetch-details
  python scripts/crawl_ikco.py --region 8 --format table

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
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests import Response, Session
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("ikco-dealers-crawler")

BASE_URL = "https://www.ikco.ir"
DEALERS_URL = f"{BASE_URL}/fa/Dealers.aspx"
DETAIL_URL_TEMPLATE = f"{BASE_URL}/Fa/DealerDetail.aspx?id={{dealer_id}}"
DEFAULT_OUTPUT = Path("data/raw_ikco_dealers.json")

FORM_PREFIX = "ctl00$ctl00$ctl00$cph1$cphContent$cphContent$AdvanceSearch1$"
SCRIPT_MANAGER = "ctl00$ctl00$ctl00$ScriptManager1"
SEARCH_RESULT_PANEL_ID = (
    "ctl00_ctl00_ctl00_cph1_cphContent_cphContent_"
    "AdvanceSearch1_SearchResult1_UpdatePanel1"
)
SEARCH_FORM_PANEL_ID = (
    "ctl00_ctl00_ctl00_cph1_cphContent_cphContent_"
    "AdvanceSearch1_UpdatePanel1"
)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_PAGE_DELAY_S = 1.0
DEFAULT_REQUEST_DELAY_MIN_S = 0.8
DEFAULT_REQUEST_DELAY_MAX_S = 2.0

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; rv:124.0) Gecko/20100101 Firefox/124.0"
    ),
]

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
POSTBACK_RE = re.compile(
    r"__doPostBack\('([^']+)'(?:,'([^']*)')?\)",
    re.IGNORECASE,
)
PROVINCE_CITY_RE = re.compile(
    r"استان\s+(.+?)\s*-\s*شهر\s+(.+?)(?:\s*[-–]\s*|\s*$)",
    re.UNICODE,
)
PHONE_RE = re.compile(
    r"(?:0\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}|0\d{10}|09\d{9})"
)
DEALER_DETAIL_ID_RE = re.compile(r"[?&]id=(\d+)", re.IGNORECASE)

ASP_NET_TOKEN_NAMES = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__LASTFOCUS",
)


@dataclass(frozen=True)
class AspNetFormState:
    """ASP.NET WebForms hidden structural tokens."""

    viewstate: str
    viewstate_generator: str
    event_validation: str
    event_target: str = ""
    event_argument: str = ""
    last_focus: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.viewstate_generator,
            "__EVENTVALIDATION": self.event_validation,
            "__EVENTTARGET": self.event_target,
            "__EVENTARGUMENT": self.event_argument,
            "__LASTFOCUS": self.last_focus,
        }


@dataclass
class SearchFilters:
    """IKCO dealer search form values (AdvanceSearch1)."""

    province_id: str | None = None
    city_id: str | None = None
    region_id: str | None = None
    sub_region_id: str | None = None
    status_id: str = "1"
    dealer_code: str | None = None
    manager_name: str | None = None
    address_query: str | None = None


@dataclass
class DealershipRecord:
    dealer_code: str | None
    dealer_name: str | None
    manager_name: str | None
    province: str | None
    city: str | None
    address: str | None
    status: str | None = None
    phone_numbers: list[str] = field(default_factory=list)
    detail_url: str | None = None
    search_region_id: str | None = None
    search_province_id: str | None = None
    source_page: int | None = None
    source: str = "ikco.ir"
    source_url: str = DEALERS_URL
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    raw_row: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class AspNetDeltaResponse:
    panels: dict[str, str]
    hidden_fields: dict[str, str]
    raw_text: str
    page_redirect: str | None = None

    @property
    def is_error_redirect(self) -> bool:
        return self.page_redirect is not None


@dataclass
class PaginateResult:
    """Outcome of a single DataPager async postback."""

    payload: dict[str, str]
    result_html: str | None
    stopped_gracefully: bool = False
    stop_reason: str | None = None


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


def extract_aspnet_form_state(soup: BeautifulSoup) -> AspNetFormState:
    """
    Parse ASP.NET structural tokens from the dealers page (or any postback page).

    Returns empty strings for missing inputs so callers can still build payloads.
    """
    values: dict[str, str] = {}
    for name in ASP_NET_TOKEN_NAMES:
        element = soup.find("input", {"name": name})
        values[name] = (element.get("value") or "") if element is not None else ""

    return AspNetFormState(
        viewstate=values["__VIEWSTATE"],
        viewstate_generator=values["__VIEWSTATEGENERATOR"],
        event_validation=values["__EVENTVALIDATION"],
        event_target=values["__EVENTTARGET"],
        event_argument=values["__EVENTARGUMENT"],
        last_focus=values["__LASTFOCUS"],
    )


def build_form_payload(soup: BeautifulSoup) -> dict[str, str]:
    """Collect all current input/select values from aspnetForm (excluding submit)."""
    form = soup.find("form", id="aspnetForm")
    if form is None:
        raise RuntimeError("aspnetForm not found in HTML")

    payload: dict[str, str] = {}
    for element in form.find_all("input"):
        name = element.get("name")
        if not name:
            continue
        input_type = (element.get("type") or "text").lower()
        if input_type == "submit":
            continue
        payload[name] = element.get("value") or ""

    for element in form.find_all("select"):
        name = element.get("name")
        if not name:
            continue
        selected = element.find("option", selected=True) or element.find("option")
        payload[name] = selected.get("value") if selected is not None else ""

    for element in form.find_all("textarea"):
        name = element.get("name")
        if name:
            payload[name] = element.get_text()

    return payload


def apply_search_filters(payload: dict[str, str], filters: SearchFilters) -> dict[str, str]:
    """Overlay SearchFilters onto a form payload."""
    updated = dict(payload)
    field_map: list[tuple[str | None, str]] = [
        (filters.province_id, f"{FORM_PREFIX}ddlProvince"),
        (filters.city_id, f"{FORM_PREFIX}ddlCity"),
        (filters.region_id, f"{FORM_PREFIX}ddlRegion"),
        (filters.sub_region_id, f"{FORM_PREFIX}ddlSlRegion"),
        (filters.status_id, f"{FORM_PREFIX}ddlStatus"),
        (filters.dealer_code, f"{FORM_PREFIX}txtID"),
        (filters.manager_name, f"{FORM_PREFIX}txtManager"),
        (filters.address_query, f"{FORM_PREFIX}txtAddress"),
    ]
    for value, field_name in field_map:
        if value is not None:
            updated[field_name] = value
    return updated


def is_aspnet_error_redirect(raw_text: str, redirect: str | None = None) -> bool:
    """True when delta or redirect target indicates IKCO error/end-of-pagination."""
    if redirect:
        lowered = redirect.lower()
        if "error.htm" in lowered or "error.aspx" in lowered:
            return True
    lowered_text = raw_text.lower()
    return "error.htm" in lowered_text or "<title>error</title>" in lowered_text


def parse_aspnet_delta_response(raw_text: str) -> AspNetDeltaResponse:
    """
    Parse an ASP.NET AJAX delta response (X-MicrosoftAjax: Delta=true).

    Extracts updatePanel HTML fragments and hiddenField token updates.
    Hidden fields (__VIEWSTATE, __EVENTVALIDATION, etc.) typically appear at the
    tail of the stream; later hiddenField entries overwrite earlier ones.
    """
    panels: dict[str, str] = {}
    hidden_fields: dict[str, str] = {}
    page_redirect: str | None = None

    if not raw_text.startswith("1|"):
        return AspNetDeltaResponse(
            panels=panels,
            hidden_fields=hidden_fields,
            raw_text=raw_text,
            page_redirect=page_redirect,
        )

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

    return AspNetDeltaResponse(
        panels=panels,
        hidden_fields=hidden_fields,
        raw_text=raw_text,
        page_redirect=page_redirect,
    )


def apply_delta_to_payload(
    payload: dict[str, str],
    delta: AspNetDeltaResponse,
) -> dict[str, str]:
    """Merge hiddenField tokens and UpdatePanel select values into a form payload."""
    refreshed = dict(payload)
    refreshed.update(delta.hidden_fields)
    for panel_id, panel_html in delta.panels.items():
        if SEARCH_FORM_PANEL_ID in panel_id:
            refreshed = merge_form_payload_from_panel_html(refreshed, panel_html)
    return refreshed


def merge_form_payload_from_panel_html(payload: dict[str, str], panel_html: str) -> dict[str, str]:
    """Refresh select values from an UpdatePanel HTML fragment."""
    updated = dict(payload)
    fragment = BeautifulSoup(panel_html, "html.parser")
    for element in fragment.find_all("select"):
        name = element.get("name")
        if not name:
            continue
        selected = element.find("option", selected=True) or element.find("option")
        updated[name] = selected.get("value") if selected is not None else ""
    return updated


def parse_province_city_from_address(address: str | None) -> tuple[str | None, str | None]:
    if not address:
        return None, None
    match = PROVINCE_CITY_RE.search(clean_farsi_text(address) or "")
    if not match:
        return None, None
    province = clean_farsi_text(match.group(1))
    city = clean_farsi_text(match.group(2))
    return province, city


def parse_search_results_html(
    html: str,
    *,
    source_page: int | None = None,
    search_region_id: str | None = None,
    search_province_id: str | None = None,
) -> list[DealershipRecord]:
    """
    Parse dealer rows from a full page or SearchResult UpdatePanel fragment.

    Expected grid columns (RTL):
      code | status | manager | dealer description | address | detail link
    """
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.find("div", id=re.compile("SearchResult1_UpdatePanel1"))
    scope: Tag | BeautifulSoup = panel if panel is not None else soup

    records: list[DealershipRecord] = []
    for row in scope.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        cell_text = [clean_farsi_text(cell.get_text(" ", strip=True)) or "" for cell in cells]
        code = normalize_dealer_code(cell_text[1])
        if not code:
            continue

        status = cell_text[2] or None
        manager_name = cell_text[3] or None
        dealer_name = cell_text[4] or None
        address = cell_text[5] or None if len(cell_text) > 5 else None
        province, city = parse_province_city_from_address(address)

        detail_url: str | None = None
        detail_link = row.find("a", href=DEALER_DETAIL_ID_RE)
        if detail_link is not None:
            href = detail_link.get("href") or ""
            detail_url = urljoin(BASE_URL, href)
            id_match = DEALER_DETAIL_ID_RE.search(href)
            if id_match and not code:
                code = id_match.group(1)

        records.append(
            DealershipRecord(
                dealer_code=code,
                dealer_name=dealer_name,
                manager_name=manager_name,
                province=province,
                city=city,
                address=address,
                status=status,
                detail_url=detail_url,
                search_region_id=search_region_id,
                search_province_id=search_province_id,
                source_page=source_page,
                raw_row={
                    "cells": cell_text,
                    "detail_href": detail_link.get("href") if detail_link else None,
                },
            )
        )

    return records


def parse_dealer_detail_phones(html: str) -> list[str]:
    """Extract phone numbers from DealerDetail.aspx markup."""
    soup = BeautifulSoup(html, "html.parser")
    phones: list[str] = []
    seen: set[str] = set()

    for match in PHONE_RE.findall(soup.get_text("\n", strip=True)):
        normalized = re.sub(r"\D", "", match)
        if len(normalized) >= 10 and normalized not in seen:
            seen.add(normalized)
            phones.append(normalized)

    return phones


def extract_pager_postbacks(html: str) -> list[tuple[str, str, str]]:
    """
    Return pager __doPostBack targets as (event_target, event_argument, label).
    """
    soup = BeautifulSoup(html, "html.parser")
    pager = soup.find("div", class_="Pager1")
    if pager is None:
        return []

    links: list[tuple[str, str, str]] = []
    for anchor in pager.find_all("a", href=True):
        match = POSTBACK_RE.search(anchor["href"])
        if not match:
            continue
        label = clean_farsi_text(anchor.get_text(strip=True)) or ""
        links.append((match.group(1), match.group(2) or "", label))
    return links


class IkcoSessionClient:
    """Session-based HTTP client for IKCO ASP.NET dealers page."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        verify_ssl: bool = False,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.session: Session = requests.Session()
        self.session.verify = verify_ssl
        self._last_form_payload: dict[str, str] = {}

    def _document_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }

    def _ajax_headers(self) -> dict[str, str]:
        return {
            **self._document_headers(),
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": DEALERS_URL,
            "Cache-Control": "no-cache",
        }

    def _post_headers(self) -> dict[str, str]:
        return {
            **self._document_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": DEALERS_URL,
        }

    @staticmethod
    def _decode_response(response: Response) -> str:
        response.encoding = "utf-8"
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
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
                return response
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

    def fetch_dealers_page(self) -> tuple[BeautifulSoup, AspNetFormState, dict[str, str]]:
        response = self._request_with_retries(
            "GET",
            DEALERS_URL,
            headers=self._document_headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to load dealers page (HTTP {response.status_code})"
            )

        html = self._decode_response(response)
        soup = BeautifulSoup(html, "html.parser")
        form_state = extract_aspnet_form_state(soup)
        payload = build_form_payload(soup)
        self._last_form_payload = payload
        return soup, form_state, payload

    def async_postback(
        self,
        payload: dict[str, str],
        *,
        event_target: str,
        event_argument: str = "",
        script_manager: str | None = None,
        allow_error_redirect: bool = False,
    ) -> AspNetDeltaResponse:
        body = dict(payload)
        body["__EVENTTARGET"] = event_target
        body["__EVENTARGUMENT"] = event_argument
        body[SCRIPT_MANAGER] = script_manager or event_target

        response = self._request_with_retries(
            "POST",
            DEALERS_URL,
            data=body,
            headers=self._ajax_headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Async postback failed (HTTP {response.status_code}) "
                f"target={event_target!r}"
            )

        raw_text = self._decode_response(response)
        delta = parse_aspnet_delta_response(raw_text)
        if delta.is_error_redirect and not allow_error_redirect:
            raise RuntimeError(
                f"Async postback redirected to error page: {delta.page_redirect}"
            )

        if not delta.is_error_redirect:
            self._last_form_payload = apply_delta_to_payload(payload, delta)
        return delta

    def full_form_post(self, payload: dict[str, str]) -> str:
        body = dict(payload)
        body.pop(SCRIPT_MANAGER, None)
        body[f"{FORM_PREFIX}btnSearch"] = "جستجو"

        response = self._request_with_retries(
            "POST",
            DEALERS_URL,
            data=body,
            headers=self._post_headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Full-form search failed (HTTP {response.status_code})"
            )

        html = self._decode_response(response)
        if "Error.htm" in html or "<title>Error</title>" in html:
            raise RuntimeError("Full-form search returned IKCO error page")

        soup = BeautifulSoup(html, "html.parser")
        self._last_form_payload = build_form_payload(soup)
        return html

    def select_province(self, payload: dict[str, str], province_id: str) -> dict[str, str]:
        refreshed = apply_search_filters(payload, SearchFilters(province_id=province_id))
        delta = self.async_postback(
            refreshed,
            event_target=f"{FORM_PREFIX}ddlProvince",
        )
        refreshed = apply_delta_to_payload(refreshed, delta)
        refreshed[f"{FORM_PREFIX}ddlProvince"] = province_id
        self._last_form_payload = refreshed
        return refreshed

    def search(self, filters: SearchFilters) -> str:
        _, _, payload = self.fetch_dealers_page()

        if filters.province_id and filters.province_id not in ("0", "-1"):
            payload = self.select_province(payload, filters.province_id)

        payload = apply_search_filters(payload, filters)
        return self.full_form_post(payload)

    def paginate(
        self,
        payload: dict[str, str],
        *,
        event_target: str,
        event_argument: str = "",
    ) -> PaginateResult:
        """
        Advance DataPager via async postback.

        On /Error.htm redirect or other pagination-end signals, returns gracefully
        without raising so callers can keep rows from earlier pages.
        """
        try:
            delta = self.async_postback(
                payload,
                event_target=event_target,
                event_argument=event_argument,
                allow_error_redirect=True,
            )
        except (RuntimeError, RequestException) as exc:
            LOGGER.warning(
                "Pagination postback failed for target=%r: %s — "
                "gracefully wrapping up this region",
                event_target,
                exc,
            )
            return PaginateResult(
                payload=payload,
                result_html=None,
                stopped_gracefully=True,
                stop_reason=str(exc),
            )

        if delta.is_error_redirect or is_aspnet_error_redirect(
            delta.raw_text,
            delta.page_redirect,
        ):
            reason = delta.page_redirect or "ASP.NET error redirect"
            LOGGER.warning(
                "Hit end of pagination or server error page (%s), "
                "gracefully wrapping up this region...",
                reason,
            )
            return PaginateResult(
                payload=payload,
                result_html=None,
                stopped_gracefully=True,
                stop_reason=reason,
            )

        if not delta.raw_text.startswith("1|") and len(delta.raw_text) < 500:
            LOGGER.warning(
                "Pagination returned unexpected short payload (%d bytes) — "
                "gracefully wrapping up this region",
                len(delta.raw_text),
            )
            return PaginateResult(
                payload=payload,
                result_html=None,
                stopped_gracefully=True,
                stop_reason="unexpected short pagination response",
            )

        refreshed = apply_delta_to_payload(payload, delta)
        self._last_form_payload = refreshed
        result_html = delta.panels.get(SEARCH_RESULT_PANEL_ID)
        return PaginateResult(payload=refreshed, result_html=result_html)

    def fetch_dealer_detail(self, dealer_id: str) -> str:
        url = DETAIL_URL_TEMPLATE.format(dealer_id=dealer_id)
        response = self._request_with_retries(
            "GET",
            url,
            headers=self._document_headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Dealer detail fetch failed for id={dealer_id} "
                f"(HTTP {response.status_code})"
            )
        return self._decode_response(response)


class IkcoDealersCrawler:
    """High-level crawl orchestrator for IKCO dealership network."""

    def __init__(
        self,
        client: IkcoSessionClient,
        *,
        request_delay_min: float = DEFAULT_REQUEST_DELAY_MIN_S,
        request_delay_max: float = DEFAULT_REQUEST_DELAY_MAX_S,
        page_delay: float = DEFAULT_PAGE_DELAY_S,
        max_pages: int | None = None,
        fetch_details: bool = False,
    ) -> None:
        self.client = client
        self.request_delay_min = request_delay_min
        self.request_delay_max = request_delay_max
        self.page_delay = page_delay
        self.max_pages = max_pages
        self.fetch_details = fetch_details

    def _sleep_between_requests(self) -> None:
        delay = random.uniform(self.request_delay_min, self.request_delay_max)
        time.sleep(delay)

    def _load_filter_options(self, soup: BeautifulSoup) -> dict[str, list[tuple[str, str]]]:
        options: dict[str, list[tuple[str, str]]] = {
            "provinces": [],
            "regions": [],
        }
        province_select = soup.find("select", id=re.compile("ddlProvince"))
        if province_select is not None:
            for option in province_select.find_all("option"):
                value = (option.get("value") or "").strip()
                label = clean_farsi_text(option.get_text(strip=True)) or ""
                if value and value not in ("0", "-1") and label:
                    options["provinces"].append((value, label))

        region_select = soup.find("select", id=re.compile("ddlRegion"))
        if region_select is not None:
            for option in region_select.find_all("option"):
                value = (option.get("value") or "").strip()
                label = clean_farsi_text(option.get_text(strip=True)) or ""
                if value and value != "0" and label:
                    options["regions"].append((value, label))

        return options

    def _collect_search_pages(
        self,
        initial_html: str,
        form_payload: dict[str, str],
        *,
        search_region_id: str | None = None,
        search_province_id: str | None = None,
    ) -> tuple[list[DealershipRecord], dict[str, str]]:
        records: list[DealershipRecord] = []
        payload = dict(form_payload)

        page_number = 1
        records.extend(
            parse_search_results_html(
                initial_html,
                source_page=page_number,
                search_region_id=search_region_id,
                search_province_id=search_province_id,
            )
        )
        LOGGER.info(
            "Search page %d: extracted %d dealer row(s)",
            page_number,
            len(records),
        )

        visited_targets: set[str] = set()
        current_html = initial_html
        while True:
            if self.max_pages is not None and page_number >= self.max_pages:
                break

            pager_links = extract_pager_postbacks(current_html)
            next_target: tuple[str, str] | None = None
            for event_target, event_argument, label in pager_links:
                key = f"{event_target}|{event_argument}"
                if key in visited_targets:
                    continue
                if label.isdigit() and int(label) == page_number + 1:
                    next_target = (event_target, event_argument)
                    break
            if next_target is None:
                for event_target, event_argument, label in pager_links:
                    if "بعد" in label or "next" in label.lower():
                        key = f"{event_target}|{event_argument}"
                        if key not in visited_targets:
                            next_target = (event_target, event_argument)
                            break
            if next_target is None:
                break

            event_target, event_argument = next_target
            visited_targets.add(f"{event_target}|{event_argument}")
            self._sleep_between_requests()
            time.sleep(self.page_delay)

            page_result = self.client.paginate(
                payload,
                event_target=event_target,
                event_argument=event_argument,
            )
            if page_result.stopped_gracefully:
                break

            payload = page_result.payload
            result_html = page_result.result_html
            if not result_html:
                LOGGER.debug(
                    "Pagination page %d returned empty result panel — stopping",
                    page_number + 1,
                )
                break

            page_number += 1
            current_html = result_html
            page_records = parse_search_results_html(
                result_html,
                source_page=page_number,
                search_region_id=search_region_id,
                search_province_id=search_province_id,
            )
            if not page_records:
                break
            records.extend(page_records)
            LOGGER.info(
                "Search page %d: extracted %d dealer row(s)",
                page_number,
                len(page_records),
            )

        return records, payload

    def _maybe_fetch_phones(self, records: list[DealershipRecord]) -> None:
        if not self.fetch_details:
            return

        for record in records:
            if not record.dealer_code:
                continue
            self._sleep_between_requests()
            try:
                detail_html = self.client.fetch_dealer_detail(record.dealer_code)
            except RuntimeError as exc:
                LOGGER.warning(
                    "Detail fetch failed for dealer_code=%s: %s",
                    record.dealer_code,
                    exc,
                )
                continue
            record.phone_numbers = parse_dealer_detail_phones(detail_html)

    def crawl_by_regions(
        self,
        region_ids: list[str] | None = None,
    ) -> list[DealershipRecord]:
        soup, _, _ = self.client.fetch_dealers_page()
        options = self._load_filter_options(soup)
        targets = options["regions"]
        if region_ids:
            allowed = set(region_ids)
            targets = [(value, label) for value, label in targets if value in allowed]

        all_records: list[DealershipRecord] = []
        for index, (region_id, region_label) in enumerate(targets):
            if index > 0:
                self._sleep_between_requests()

            LOGGER.info("Searching regional office %s (%s)", region_id, region_label)
            try:
                html = self.client.search(
                    SearchFilters(region_id=region_id, status_id="1")
                )
            except RuntimeError as exc:
                LOGGER.warning(
                    "Region %s (%s) search failed: %s — skipping",
                    region_id,
                    region_label,
                    exc,
                )
                continue

            soup = BeautifulSoup(html, "html.parser")
            payload = build_form_payload(soup)
            payload = apply_search_filters(
                payload,
                SearchFilters(region_id=region_id, status_id="1"),
            )
            region_records, _ = self._collect_search_pages(
                html,
                payload,
                search_region_id=region_id,
            )
            self._maybe_fetch_phones(region_records)
            all_records.extend(region_records)
            LOGGER.info(
                "Region %s (%s): total %d record(s) so far",
                region_id,
                region_label,
                len(region_records),
            )

        return self._dedupe_records(all_records)

    def crawl_by_provinces(
        self,
        province_ids: list[str] | None = None,
    ) -> list[DealershipRecord]:
        soup, _, _ = self.client.fetch_dealers_page()
        options = self._load_filter_options(soup)
        targets = options["provinces"]
        if province_ids:
            allowed = set(province_ids)
            targets = [(value, label) for value, label in targets if value in allowed]

        all_records: list[DealershipRecord] = []
        for index, (province_id, province_label) in enumerate(targets):
            if index > 0:
                self._sleep_between_requests()

            LOGGER.info("Searching province %s (%s)", province_id, province_label)
            try:
                html = self.client.search(
                    SearchFilters(province_id=province_id, status_id="1")
                )
            except RuntimeError as exc:
                LOGGER.warning(
                    "Province %s (%s) search failed: %s — skipping",
                    province_id,
                    province_label,
                    exc,
                )
                continue

            soup = BeautifulSoup(html, "html.parser")
            payload = build_form_payload(soup)
            payload = apply_search_filters(
                payload,
                SearchFilters(province_id=province_id, status_id="1"),
            )
            province_records, _ = self._collect_search_pages(
                html,
                payload,
                search_province_id=province_id,
            )
            self._maybe_fetch_phones(province_records)
            all_records.extend(province_records)

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


def upsert_dealer_records(db: Any, records: list[DealershipRecord]) -> dict[str, int]:
    """
    Placeholder persistence hook for IKCO dealership rows.

    Expected target schema (to be added via Alembic migration):

        scraped_dealer (
            id              SERIAL PRIMARY KEY,
            dealer_code     VARCHAR(32) NOT NULL,
            dealer_name     TEXT,
            manager_name    TEXT,
            province        VARCHAR(120),
            city            VARCHAR(120),
            address         TEXT,
            phone_numbers   JSONB,
            status          VARCHAR(64),
            source          VARCHAR(64) NOT NULL,
            source_url      TEXT,
            detail_url      TEXT,
            raw_row         JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (source, dealer_code)
        )

    Duplicate avoidance: upsert on (source, dealer_code).

    Replace this stub once the repository layer exists, e.g.:

        repo = ScrapedDealerRepository(db)
        for record in records:
            repo.upsert_by_code(source=record.source, payload=asdict(record))
    """
    inserted = 0
    updated = 0
    skipped = 0

    for record in records:
        if not record.dealer_code:
            skipped += 1
            LOGGER.debug("Skipping row without dealer_code: %r", record.dealer_name)
            continue

        LOGGER.debug(
            "upsert placeholder: source=%s dealer_code=%s name=%r",
            record.source,
            record.dealer_code,
            record.dealer_name,
        )
        inserted += 1

    LOGGER.info(
        "upsert_dealer_records placeholder complete "
        "(inserted=%d updated=%d skipped=%d)",
        inserted,
        updated,
        skipped,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def print_table(records: list[DealershipRecord]) -> None:
    if not records:
        print("No dealerships extracted.")
        return

    headers = ("code", "manager", "province", "city", "dealer_name")
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.dealer_code or "-",
                (record.manager_name or "-")[:34],
                (record.province or "-")[:14],
                (record.city or "-")[:16],
                (record.dealer_name or "-")[:36],
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


def write_json(path: Path, records: list[DealershipRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Iran Khodro (ikco.ir) dealership network."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Write JSON array to this path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--strategy",
        choices=("region", "province"),
        default="region",
        help=(
            "Crawl driver: regional offices (reliable) or provinces "
            "(requires async province postback; may error server-side)"
        ),
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit to specific ddlRegion value(s), e.g. --region 8",
    )
    parser.add_argument(
        "--province",
        action="append",
        dest="provinces",
        help="Limit to specific ddlProvince value(s), e.g. --province 4",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Max result pages per search query (default: all pages)",
    )
    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="Fetch DealerDetail.aspx for phone numbers (extra HTTP calls)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=DEFAULT_PAGE_DELAY_S,
        help=f"Delay between pagination requests (default: {DEFAULT_PAGE_DELAY_S})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "table", "both"),
        default="both",
        help="Console output format (default: both)",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Call upsert_dealer_records placeholder after crawl",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _configure_logging(args.verbose)

    client = IkcoSessionClient(timeout=args.timeout)
    crawler = IkcoDealersCrawler(
        client,
        page_delay=args.page_delay,
        max_pages=args.max_pages,
        fetch_details=args.fetch_details,
    )

    try:
        if args.strategy == "region":
            records = crawler.crawl_by_regions(region_ids=args.regions)
        else:
            records = crawler.crawl_by_provinces(province_ids=args.provinces)
    except (RequestException, RuntimeError) as exc:
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    write_json(args.output, records)
    LOGGER.info("Wrote %d records to %s", len(records), args.output.resolve())

    if args.persist:
        upsert_dealer_records(db=None, records=records)

    if args.format in ("table", "both"):
        print_table(records)

    if args.format in ("json", "both"):
        print(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
