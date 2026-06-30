#!/usr/bin/env python3
"""
Crawler: Diar Khodro (دیار خودرو) sales dealership network.

The public page renders dealership rows client-side from ``/js/dealership/sales.js``.
This script fetches the HTML shell plus that script, rebuilds the rendered DOM the
browser would produce, and extracts every available field (text, links, images,
data-* attributes) with per-field error isolation.

Usage:
  python scripts/crawl_diar_khodro.py
  python scripts/crawl_diar_khodro.py --output data/raw_diar_khodro.json -v

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("diar-khodro-crawler")

TARGET_URL = "https://diar-khodro.com/dealership/sales-dealerships"
SALES_JS_PATH = "/js/dealership/sales.js"
DEFAULT_OUTPUT = Path("data/raw_diar_khodro.json")
REQUEST_TIMEOUT_S = 25

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

ACTIVE_HEADERS = [
    "#",
    "نام نمایندگی",
    "استان",
    "شهر",
    "وضعیت",
    "کد",
    "آدرس",
    "ساعت کاری",
    "تلفن",
    "لوکیشن",
]

SUSPENDED_HEADERS = [
    "#",
    "نام نمایندگی",
    "کد نمایندگی",
    "استان",
    "شهر",
]

JS_FIELD_RE = re.compile(
    r'(\w+)\s*:\s*(?:"((?:\\.|[^"\\])*)"|(false|true)|(\d+))',
    re.DOTALL,
)


class SafeField:
    """Run a callable and return its result or a fallback without raising."""

    @staticmethod
    def get(
        getter: Any,
        fallback: Any = None,
        field_name: str = "unknown",
    ) -> Any:
        try:
            return getter()
        except Exception as exc:  # noqa: BLE001 — intentional per-field isolation
            LOGGER.debug("Field %s extraction failed: %s", field_name, exc)
            return fallback


class HttpClient:
    """Thin requests wrapper with consistent headers and error reporting."""

    def __init__(self, base_url: str, timeout_s: int = REQUEST_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers.update(BROWSER_HEADERS)

    def get_text(self, url: str) -> str:
        response = self._session.get(url, timeout=self._timeout_s, allow_redirects=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def resolve(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return urljoin(f"{self._base_url}/", path_or_url.lstrip("/"))


class JsDealershipParser:
    """Parse ``dealers`` and ``suspendedDealers`` arrays from sales.js."""

    @staticmethod
    def _strip_js_comments(text: str) -> str:
        """Remove full-line ``//`` comments without touching ``https://`` URLs."""
        kept_lines: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith("//"):
                continue
            kept_lines.append(line)
        return "\n".join(kept_lines)

    @classmethod
    def _extract_const_array(cls, js_content: str, const_name: str) -> str:
        marker = f"const {const_name} ="
        start_idx = js_content.index(marker) + len(marker)
        while start_idx < len(js_content) and js_content[start_idx] != "[":
            start_idx += 1
        if start_idx >= len(js_content):
            raise ValueError(f"Array for {const_name} not found")

        depth = 0
        in_string = False
        escape = False
        for idx in range(start_idx, len(js_content)):
            char = js_content[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return js_content[start_idx : idx + 1]
        raise ValueError(f"Unterminated array for {const_name}")

    @classmethod
    def _parse_js_object(cls, object_text: str) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for match in JS_FIELD_RE.finditer(object_text):
            key = match.group(1)
            if match.group(2) is not None:
                value: Any = match.group(2)
            elif match.group(3) is not None:
                value = match.group(3) == "true"
            else:
                value = int(match.group(4))
            record[key] = value
        return record

    @classmethod
    def _parse_js_object_array(cls, array_text: str) -> list[dict[str, Any]]:
        cleaned = cls._strip_js_comments(array_text)
        objects: list[dict[str, Any]] = []
        depth = 0
        in_string = False
        escape = False
        object_start: int | None = None

        for idx, char in enumerate(cleaned):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                if depth == 0:
                    object_start = idx
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and object_start is not None:
                    chunk = cleaned[object_start : idx + 1]
                    parsed = cls._parse_js_object(chunk)
                    if parsed:
                        objects.append(parsed)
                    object_start = None

        return objects

    @classmethod
    def parse_sales_js(cls, js_content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        dealers_array = cls._extract_const_array(js_content, "dealers")
        suspended_array = cls._extract_const_array(js_content, "suspendedDealers")
        dealers = cls._parse_js_object_array(dealers_array)
        suspended = cls._parse_js_object_array(suspended_array)
        return dealers, suspended


class DealershipDomRenderer:
    """Replicate client-side ``renderTableInfo`` / ``renderSuspended`` in Python."""

    @staticmethod
    def _location_cell(location: Any) -> str:
        if not location:
            return '<i class="fa-solid fa-minus"></i>'
        href = html_lib.escape(str(location), quote=True)
        return (
            f"<a class='loc-link' href='{href}' target='_blank'>"
            "<i class='fas fa-map-marked-alt loc-icon'></i></a>"
        )

    @classmethod
    def render_active_rows(cls, dealers: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for index, item in enumerate(dealers, start=1):
            rows.append(
                "<div class='tr'>"
                f"<div>{index}</div>"
                f"<div>{item.get('title', '')}</div>"
                f"<div>{item.get('state', '')}</div>"
                f"<div>{item.get('city', '')}</div>"
                f"<div>{item.get('status', '')}</div>"
                f"<div>{item.get('code', '')}</div>"
                f"<div>{item.get('address', '')}</div>"
                f"<div>{item.get('workingHours', '')}</div>"
                f"<div>{item.get('phone', '')}</div>"
                f"<div>{cls._location_cell(item.get('location'))}</div>"
                "</div>"
            )
        return "".join(rows)

    @classmethod
    def render_active_header(cls) -> str:
        header_cells = "".join(f"<div>{label}</div>" for label in ACTIVE_HEADERS)
        return f"<div class='tr tr-head'>{header_cells}</div>"

    @classmethod
    def render_suspended_rows(cls, suspended: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for index, item in enumerate(suspended, start=1):
            rows.append(
                "<div class='tr'>"
                f"<div>{index}</div>"
                f"<div>{item.get('title', '')}</div>"
                f"<div>{item.get('code', '')}</div>"
                f"<div>{item.get('state', '')}</div>"
                f"<div>{item.get('city', '')}</div>"
                "</div>"
            )
        return "".join(rows)

    @classmethod
    def inject_into_page(
        cls,
        page_html: str,
        dealers: list[dict[str, Any]],
        suspended: list[dict[str, Any]],
    ) -> BeautifulSoup:
        soup = BeautifulSoup(page_html, "html.parser")

        topper = soup.find(id="topper")
        if topper is not None:
            topper.clear()
            topper["class"] = [c for c in topper.get("class", []) if c != "dis-none"]
            if "dis-block" not in topper.get("class", []):
                topper.get("class", []).append("dis-block")
            topper.append(
                BeautifulSoup(
                    cls.render_active_header() + cls.render_active_rows(dealers),
                    "html.parser",
                )
            )

        suspended_table = soup.find(id="suspendedTable")
        if suspended_table is not None:
            for child in list(suspended_table.find_all("div", class_="tr")):
                if "th" not in child.get("class", []):
                    child.decompose()
            suspended_table.append(
                BeautifulSoup(cls.render_suspended_rows(suspended), "html.parser")
            )

        return soup


class HtmlNodeExtractor:
    """Recursively capture text, links, images, and every attribute on a tag."""

    @classmethod
    def extract_tag(cls, tag: Tag) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tag": SafeField.get(lambda: tag.name, field_name="tag.name"),
            "text": SafeField.get(
                lambda: tag.get_text(" ", strip=True) or None,
                field_name="tag.text",
            ),
            "attributes": SafeField.get(
                lambda: cls._extract_attributes(tag),
                fallback={},
                field_name="tag.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: cls._extract_data_attributes(tag),
                fallback={},
                field_name="tag.data_attributes",
            ),
            "links": SafeField.get(
                lambda: cls._extract_links(tag),
                fallback=[],
                field_name="tag.links",
            ),
            "images": SafeField.get(
                lambda: cls._extract_images(tag),
                fallback=[],
                field_name="tag.images",
            ),
            "children": SafeField.get(
                lambda: cls._extract_children(tag),
                fallback=[],
                field_name="tag.children",
            ),
        }
        return result

    @staticmethod
    def _extract_attributes(tag: Tag) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for key, value in tag.attrs.items():
            if isinstance(value, list):
                attrs[str(key)] = " ".join(str(v) for v in value)
            else:
                attrs[str(key)] = str(value)
        return attrs

    @staticmethod
    def _extract_data_attributes(tag: Tag) -> dict[str, str]:
        data_attrs: dict[str, str] = {}
        for key, value in tag.attrs.items():
            if not str(key).startswith("data-"):
                continue
            if isinstance(value, list):
                data_attrs[str(key)] = " ".join(str(v) for v in value)
            else:
                data_attrs[str(key)] = str(value)
        return data_attrs

    @classmethod
    def _extract_links(cls, tag: Tag) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for anchor in tag.find_all("a", href=True):
            links.append(
                {
                    "href": SafeField.get(
                        lambda a=anchor: a.get("href"),
                        field_name="link.href",
                    ),
                    "text": SafeField.get(
                        lambda a=anchor: a.get_text(" ", strip=True) or None,
                        field_name="link.text",
                    ),
                    "target": SafeField.get(
                        lambda a=anchor: a.get("target"),
                        field_name="link.target",
                    ),
                    "class": SafeField.get(
                        lambda a=anchor: " ".join(a.get("class", [])) or None,
                        field_name="link.class",
                    ),
                    "attributes": SafeField.get(
                        lambda a=anchor: cls._extract_attributes(a),
                        fallback={},
                        field_name="link.attributes",
                    ),
                    "data_attributes": SafeField.get(
                        lambda a=anchor: cls._extract_data_attributes(a),
                        fallback={},
                        field_name="link.data_attributes",
                    ),
                }
            )
        return links

    @classmethod
    def _extract_images(cls, tag: Tag) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for image in tag.find_all("img"):
            images.append(
                {
                    "src": SafeField.get(
                        lambda i=image: i.get("src"),
                        field_name="image.src",
                    ),
                    "alt": SafeField.get(
                        lambda i=image: i.get("alt"),
                        field_name="image.alt",
                    ),
                    "attributes": SafeField.get(
                        lambda i=image: cls._extract_attributes(i),
                        fallback={},
                        field_name="image.attributes",
                    ),
                    "data_attributes": SafeField.get(
                        lambda i=image: cls._extract_data_attributes(i),
                        fallback={},
                        field_name="image.data_attributes",
                    ),
                }
            )
        return images

    @staticmethod
    def _extract_children(tag: Tag) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for child in tag.children:
            if isinstance(child, Tag):
                children.append(HtmlNodeExtractor.extract_tag(child))
        return children


class DealershipTableExtractor:
    """Map rendered table rows to structured dealership dictionaries."""

    @staticmethod
    def _row_cells(row: Tag) -> list[str]:
        cells: list[str] = []
        for cell in row.find_all("div", recursive=False):
            cells.append(cell.get_text(" ", strip=True))
        return cells

    @classmethod
    def _row_columns(
        cls,
        headers: list[str],
        row: Tag,
    ) -> dict[str, Any]:
        cells = cls._row_cells(row)
        columns: dict[str, Any] = {}
        for idx, header in enumerate(headers):
            columns[header] = SafeField.get(
                lambda i=idx: cells[i] if i < len(cells) else None,
                field_name=f"column.{header}",
            )
        return columns

    @classmethod
    def extract_active_dealership(
        cls,
        row: Tag,
        raw_js: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        return {
            "record_type": "active",
            "index": index,
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_js": deepcopy(raw_js),
            "table_columns": SafeField.get(
                lambda: cls._row_columns(ACTIVE_HEADERS, row),
                fallback={},
                field_name="active.table_columns",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["links"],
                fallback=[],
                field_name="active.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["images"],
                fallback=[],
                field_name="active.images",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["data_attributes"],
                fallback={},
                field_name="active.data_attributes",
            ),
            "html_row": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row),
                fallback={},
                field_name="active.html_row",
            ),
            "location_href": SafeField.get(
                lambda: (
                    row.find("a", class_="loc-link").get("href")
                    if row.find("a", class_="loc-link")
                    else None
                ),
                field_name="active.location_href",
            ),
            "fax": SafeField.get(
                lambda: raw_js.get("fax"),
                field_name="active.fax",
            ),
        }

    @classmethod
    def extract_suspended_dealership(
        cls,
        row: Tag,
        raw_js: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        return {
            "record_type": "suspended",
            "index": index,
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_js": deepcopy(raw_js),
            "table_columns": SafeField.get(
                lambda: cls._row_columns(SUSPENDED_HEADERS, row),
                fallback={},
                field_name="suspended.table_columns",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["links"],
                fallback=[],
                field_name="suspended.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["images"],
                fallback=[],
                field_name="suspended.images",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row)["data_attributes"],
                fallback={},
                field_name="suspended.data_attributes",
            ),
            "html_row": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row),
                fallback={},
                field_name="suspended.html_row",
            ),
        }


class DiarKhodroCrawler:
    """Orchestrate fetch, DOM rebuild, extraction, and JSON export."""

    def __init__(self, output_path: Path) -> None:
        parsed = urlparse(TARGET_URL)
        self._http = HttpClient(f"{parsed.scheme}://{parsed.netloc}")
        self._output_path = output_path

    def _fetch_page_html(self) -> str:
        LOGGER.info("Fetching page: %s", TARGET_URL)
        try:
            return self._http.get_text(TARGET_URL)
        except SSLError as exc:
            raise RuntimeError(f"SSL error while fetching page: {exc}") from exc
        except (Timeout, ReadTimeout) as exc:
            raise RuntimeError(f"Timeout while fetching page: {exc}") from exc
        except ConnectionError as exc:
            raise RuntimeError(f"Connection error while fetching page: {exc}") from exc
        except RequestException as exc:
            raise RuntimeError(f"HTTP error while fetching page: {exc}") from exc

    def _fetch_sales_js(self, page_html: str) -> str:
        soup = BeautifulSoup(page_html, "html.parser")
        script_src: str | None = None
        for script in soup.find_all("script", src=True):
            src = script.get("src", "")
            if "dealership/sales.js" in src:
                script_src = src
                break
        if not script_src:
            script_src = SALES_JS_PATH

        js_url = self._http.resolve(script_src)
        LOGGER.info("Fetching dealership data script: %s", js_url)
        try:
            return self._http.get_text(js_url)
        except RequestException as exc:
            raise RuntimeError(f"Failed to fetch sales.js: {exc}") from exc

    def crawl(self) -> dict[str, Any]:
        page_html = self._fetch_page_html()
        sales_js = self._fetch_sales_js(page_html)

        dealers, suspended = JsDealershipParser.parse_sales_js(sales_js)
        LOGGER.info(
            "Parsed %d active and %d suspended dealerships from sales.js",
            len(dealers),
            len(suspended),
        )

        soup = DealershipDomRenderer.inject_into_page(page_html, dealers, suspended)
        records: list[dict[str, Any]] = []

        topper = soup.find(id="topper")
        if topper is not None:
            data_rows = [
                row
                for row in topper.find_all("div", class_="tr")
                if "tr-head" not in row.get("class", [])
            ]
            for idx, (row, raw) in enumerate(zip(data_rows, dealers, strict=False), start=1):
                records.append(
                    SafeField.get(
                        lambda r=row, js=raw, i=idx: DealershipTableExtractor.extract_active_dealership(
                            r, js, i
                        ),
                        fallback={
                            "record_type": "active",
                            "index": idx,
                            "error": "failed_to_extract_row",
                            "raw_js": deepcopy(raw),
                        },
                        field_name=f"active_record.{idx}",
                    )
                )

        suspended_table = soup.find(id="suspendedTable")
        if suspended_table is not None:
            data_rows = [
                row
                for row in suspended_table.find_all("div", class_="tr")
                if "th" not in row.get("class", [])
            ]
            for idx, (row, raw) in enumerate(zip(data_rows, suspended, strict=False), start=1):
                records.append(
                    SafeField.get(
                        lambda r=row, js=raw, i=idx: DealershipTableExtractor.extract_suspended_dealership(
                            r, js, i
                        ),
                        fallback={
                            "record_type": "suspended",
                            "index": idx,
                            "error": "failed_to_extract_row",
                            "raw_js": deepcopy(raw),
                        },
                        field_name=f"suspended_record.{idx}",
                    )
                )

        page_meta = SafeField.get(
            lambda: self._extract_page_metadata(soup),
            fallback={},
            field_name="page_metadata",
        )

        payload: dict[str, Any] = {
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "total_records": len(records),
            "active_count": len(dealers),
            "suspended_count": len(suspended),
            "page_metadata": page_meta,
            "dealerships": records,
        }
        return payload

    @staticmethod
    def _extract_page_metadata(soup: BeautifulSoup) -> dict[str, Any]:
        title_tag = soup.find("title")
        return {
            "title": SafeField.get(
                lambda: title_tag.get_text(strip=True) if title_tag else None,
                field_name="page.title",
            ),
            "page_links": SafeField.get(
                lambda: [
                    {
                        "href": anchor.get("href"),
                        "text": anchor.get_text(" ", strip=True) or None,
                    }
                    for anchor in soup.find("main").find_all("a", href=True)
                ]
                if soup.find("main")
                else [],
                fallback=[],
                field_name="page.links",
            ),
            "page_images": SafeField.get(
                lambda: [
                    {
                        "src": image.get("src"),
                        "alt": image.get("alt"),
                    }
                    for image in soup.find("main").find_all("img")
                ]
                if soup.find("main")
                else [],
                fallback=[],
                field_name="page.images",
            ),
        }

    def save(self, payload: dict[str, Any]) -> Path:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        LOGGER.info("Saved %d records to %s", payload["total_records"], self._output_path)
        return self._output_path


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl Diar Khodro sales dealerships into raw JSON."
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _configure_logging(args.verbose)

    crawler = DiarKhodroCrawler(output_path=args.output)
    try:
        payload = crawler.crawl()
        output_file = crawler.save(payload)
    except Exception as exc:  # noqa: BLE001 — top-level guard for CLI
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    print(
        f"[+] Done: {payload['total_records']} records "
        f"({payload['active_count']} active, {payload['suspended_count']} suspended) "
        f"-> {output_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
