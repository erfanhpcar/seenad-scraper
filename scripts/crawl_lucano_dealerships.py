#!/usr/bin/env python3
"""
Crawler: Ecodalucano (اکودا لوکانو) sales dealership network.

Raw-data strategy: fetch the full HTML page, dynamically parse every table / div /
list structure, scan all inline scripts for embedded JSON or JS variables, and
persist the untouched extraction output.

Usage:
  python scripts/crawl_lucano_dealerships.py
  python scripts/crawl_lucano_dealerships.py --output data/raw_lucano_dealerships.json -v

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("lucano-dealerships-crawler")

TARGET_URL = "https://ecodalucano.com/dealerships/"
DEFAULT_OUTPUT = Path("data/raw_lucano_dealerships.json")
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

DIV_CARD_HINTS = (
    "dealership",
    "dealer",
    "agent",
    "branch",
    "representative",
    "نمایندگی",
    "نماینده",
    "card",
    "tablepress",
)

JS_ASSIGNMENT_RE = re.compile(
    r"(?:var|const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*;",
    re.MULTILINE,
)


class SafeField:
    """Run a callable and return its result or a fallback without raising."""

    @staticmethod
    def get(
        getter: Callable[[], Any],
        fallback: Any = None,
        field_name: str = "unknown",
    ) -> Any:
        try:
            return getter()
        except Exception as exc:  # noqa: BLE001 — intentional per-field isolation
            LOGGER.debug("Field %s extraction failed: %s", field_name, exc)
            return fallback


class HttpClient:
    """Fetch HTML with a standard User-Agent and UTF-8 decoding."""

    def __init__(self, timeout_s: int = REQUEST_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers.update(BROWSER_HEADERS)

    def get_html(self, url: str) -> tuple[str, dict[str, Any]]:
        response = self._session.get(url, timeout=self._timeout_s, allow_redirects=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        meta = {
            "status_code": response.status_code,
            "final_url": response.url,
            "encoding": response.encoding,
            "content_type": response.headers.get("Content-Type"),
            "content_length": len(response.content),
        }
        return response.text, meta


class HtmlNodeExtractor:
    """Recursively capture text, inner HTML, links, images, and attributes."""

    @classmethod
    def extract_tag(cls, tag: Tag) -> dict[str, Any]:
        return {
            "tag": SafeField.get(lambda: tag.name, field_name="tag.name"),
            "text": SafeField.get(
                lambda: tag.get_text("", strip=False) or None,
                field_name="tag.text",
            ),
            "text_stripped": SafeField.get(
                lambda: tag.get_text(" ", strip=True) or None,
                field_name="tag.text_stripped",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in tag.contents) or None,
                field_name="tag.inner_html",
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
            "iframes": SafeField.get(
                lambda: cls._extract_iframes(tag),
                fallback=[],
                field_name="tag.iframes",
            ),
            "children": SafeField.get(
                lambda: cls._extract_children(tag),
                fallback=[],
                field_name="tag.children",
            ),
        }

    @staticmethod
    def _extract_attributes(tag: Tag) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for key, value in tag.attrs.items():
            if isinstance(value, list):
                attrs[str(key)] = " ".join(str(item) for item in value)
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
                data_attrs[str(key)] = " ".join(str(item) for item in value)
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
                        lambda node=anchor: node.get("href"),
                        field_name="link.href",
                    ),
                    "text": SafeField.get(
                        lambda node=anchor: node.get_text("", strip=False) or None,
                        field_name="link.text",
                    ),
                    "text_stripped": SafeField.get(
                        lambda node=anchor: node.get_text(" ", strip=True) or None,
                        field_name="link.text_stripped",
                    ),
                    "target": SafeField.get(
                        lambda node=anchor: node.get("target"),
                        field_name="link.target",
                    ),
                    "rel": SafeField.get(
                        lambda node=anchor: node.get("rel"),
                        field_name="link.rel",
                    ),
                    "attributes": SafeField.get(
                        lambda node=anchor: cls._extract_attributes(node),
                        fallback={},
                        field_name="link.attributes",
                    ),
                    "data_attributes": SafeField.get(
                        lambda node=anchor: cls._extract_data_attributes(node),
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
                        lambda node=image: node.get("src"),
                        field_name="image.src",
                    ),
                    "alt": SafeField.get(
                        lambda node=image: node.get("alt"),
                        field_name="image.alt",
                    ),
                    "attributes": SafeField.get(
                        lambda node=image: cls._extract_attributes(node),
                        fallback={},
                        field_name="image.attributes",
                    ),
                    "data_attributes": SafeField.get(
                        lambda node=image: cls._extract_data_attributes(node),
                        fallback={},
                        field_name="image.data_attributes",
                    ),
                }
            )
        return images

    @classmethod
    def _extract_iframes(cls, tag: Tag) -> list[dict[str, Any]]:
        iframes: list[dict[str, Any]] = []
        for frame in tag.find_all("iframe"):
            iframes.append(
                {
                    "src": SafeField.get(
                        lambda node=frame: node.get("src"),
                        field_name="iframe.src",
                    ),
                    "title": SafeField.get(
                        lambda node=frame: node.get("title"),
                        field_name="iframe.title",
                    ),
                    "attributes": SafeField.get(
                        lambda node=frame: cls._extract_attributes(node),
                        fallback={},
                        field_name="iframe.attributes",
                    ),
                    "data_attributes": SafeField.get(
                        lambda node=frame: cls._extract_data_attributes(node),
                        fallback={},
                        field_name="iframe.data_attributes",
                    ),
                }
            )
        return iframes

    @staticmethod
    def _extract_children(tag: Tag) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for child in tag.children:
            if isinstance(child, Tag):
                children.append(HtmlNodeExtractor.extract_tag(child))
        return children


class DynamicStructureExtractor:
    """Parse tables, div cards, and list items without filtering content."""

    @classmethod
    def extract_tables(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        for index, table in enumerate(soup.find_all("table"), start=1):
            tables.append(
                SafeField.get(
                    lambda tbl=table, i=index: cls._extract_table(tbl, i),
                    fallback={"table_index": index, "error": "table_extraction_failed"},
                    field_name=f"table.{index}",
                )
            )
        return tables

    @classmethod
    def _extract_table(cls, table: Tag, table_index: int) -> dict[str, Any]:
        headers: list[dict[str, Any]] = []
        for row in table.find_all("tr"):
            header_cells = row.find_all("th")
            if header_cells:
                for cell in header_cells:
                    headers.append(cls._extract_cell(cell))
                break

        rows: list[dict[str, Any]] = []
        for row_index, row in enumerate(table.find_all("tr"), start=1):
            if row.find("th") and not row.find("td"):
                continue
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            rows.append(
                SafeField.get(
                    lambda r=row, ri=row_index: cls._extract_table_row(r, ri, headers),
                    fallback={"row_index": row_index, "error": "row_extraction_failed"},
                    field_name=f"table.{table_index}.row.{row_index}",
                )
            )

        return {
            "table_index": table_index,
            "table_id": SafeField.get(lambda: table.get("id"), field_name="table.id"),
            "table_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(table),
                fallback={},
                field_name="table.attributes",
            ),
            "table_data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(table),
                fallback={},
                field_name="table.data_attributes",
            ),
            "headers": headers,
            "rows": rows,
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(table),
                fallback=[],
                field_name="table.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(table),
                fallback=[],
                field_name="table.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(table),
                fallback=[],
                field_name="table.iframes",
            ),
            "html_table": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(table),
                fallback={},
                field_name="table.html_table",
            ),
        }

    @classmethod
    def _extract_table_row(
        cls,
        row: Tag,
        row_index: int,
        headers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cells = [cls._extract_cell(cell) for cell in row.find_all(["td", "th"])]
        columns: list[dict[str, Any]] = []
        for col_index, cell in enumerate(cells):
            header_label = None
            if col_index < len(headers):
                header_label = headers[col_index].get("text_stripped")
            columns.append(
                {
                    "column_index": col_index + 1,
                    "header_label": header_label,
                    "cell": cell,
                }
            )

        return {
            "row_index": row_index,
            "row_class": SafeField.get(
                lambda: " ".join(row.get("class", [])) or None,
                field_name="row.class",
            ),
            "row_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(row),
                fallback={},
                field_name="row.attributes",
            ),
            "row_data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(row),
                fallback={},
                field_name="row.data_attributes",
            ),
            "columns": columns,
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(row),
                fallback=[],
                field_name="row.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(row),
                fallback=[],
                field_name="row.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(row),
                fallback=[],
                field_name="row.iframes",
            ),
            "html_row": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(row),
                fallback={},
                field_name="row.html_row",
            ),
        }

    @classmethod
    def _extract_cell(cls, cell: Tag) -> dict[str, Any]:
        return {
            "text": SafeField.get(
                lambda: cell.get_text("", strip=False) or None,
                field_name="cell.text",
            ),
            "text_stripped": SafeField.get(
                lambda: cell.get_text(" ", strip=True) or None,
                field_name="cell.text_stripped",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in cell.contents) or None,
                field_name="cell.inner_html",
            ),
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(cell),
                fallback={},
                field_name="cell.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(cell),
                fallback={},
                field_name="cell.data_attributes",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(cell),
                fallback=[],
                field_name="cell.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(cell),
                fallback=[],
                field_name="cell.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(cell),
                fallback=[],
                field_name="cell.iframes",
            ),
        }

    @classmethod
    def extract_div_blocks(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for div in soup.find_all("div"):
            if id(div) in seen_ids:
                continue
            if not cls._is_candidate_div(div):
                continue
            seen_ids.add(id(div))
            blocks.append(
                SafeField.get(
                    lambda node=div: cls._extract_div_block(node),
                    fallback={"error": "div_extraction_failed"},
                    field_name="div_block",
                )
            )
        return blocks

    @staticmethod
    def _is_candidate_div(div: Tag) -> bool:
        class_text = " ".join(div.get("class", [])).lower()
        if any(hint in class_text for hint in DIV_CARD_HINTS):
            return True

        for attr_name in ("id", "data-widget_type", "data-element_type"):
            attr_value = str(div.get(attr_name, "")).lower()
            if any(hint in attr_value for hint in DIV_CARD_HINTS):
                return True

        data_settings = str(div.get("data-settings", "")).lower()
        return any(hint in data_settings for hint in DIV_CARD_HINTS)

    @classmethod
    def _extract_div_block(cls, div: Tag) -> dict[str, Any]:
        return {
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(div),
                fallback={},
                field_name="div.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(div),
                fallback={},
                field_name="div.data_attributes",
            ),
            "text": SafeField.get(
                lambda: div.get_text("", strip=False) or None,
                field_name="div.text",
            ),
            "text_stripped": SafeField.get(
                lambda: div.get_text(" ", strip=True) or None,
                field_name="div.text_stripped",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in div.contents) or None,
                field_name="div.inner_html",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(div),
                fallback=[],
                field_name="div.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(div),
                fallback=[],
                field_name="div.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(div),
                fallback=[],
                field_name="div.iframes",
            ),
            "html_div": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(div),
                fallback={},
                field_name="div.html_div",
            ),
        }

    @classmethod
    def extract_list_items(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        content_root = (
            soup.find("div", attrs={"data-elementor-type": "wp-page"})
            or soup.find("main")
            or soup.body
        )
        if content_root is None:
            return []

        items: list[dict[str, Any]] = []
        for index, item in enumerate(content_root.find_all("li"), start=1):
            items.append(
                SafeField.get(
                    lambda node=item, i=index: cls._extract_list_item(node, i),
                    fallback={"list_index": index, "error": "list_item_extraction_failed"},
                    field_name=f"list_item.{index}",
                )
            )
        return items

    @classmethod
    def _extract_list_item(cls, item: Tag, list_index: int) -> dict[str, Any]:
        return {
            "list_index": list_index,
            "text": SafeField.get(
                lambda: item.get_text("", strip=False) or None,
                field_name="li.text",
            ),
            "text_stripped": SafeField.get(
                lambda: item.get_text(" ", strip=True) or None,
                field_name="li.text_stripped",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in item.contents) or None,
                field_name="li.inner_html",
            ),
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(item),
                fallback={},
                field_name="li.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(item),
                fallback={},
                field_name="li.data_attributes",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(item),
                fallback=[],
                field_name="li.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(item),
                fallback=[],
                field_name="li.images",
            ),
            "html_li": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(item),
                fallback={},
                field_name="li.html_li",
            ),
        }


class ScriptLayerScanner:
    """Inspect every script tag for JSON payloads or hard-coded JS variables."""

    @classmethod
    def scan(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        scripts: list[dict[str, Any]] = []
        for index, script in enumerate(soup.find_all("script"), start=1):
            scripts.append(
                SafeField.get(
                    lambda node=script, i=index: cls._extract_script(node, i),
                    fallback={"script_index": index, "error": "script_extraction_failed"},
                    field_name=f"script.{index}",
                )
            )
        return scripts

    @classmethod
    def _extract_script(cls, script: Tag, script_index: int) -> dict[str, Any]:
        raw_text = script.string if script.string is not None else script.get_text()
        raw_text = raw_text or ""

        parsed_json = SafeField.get(
            lambda: cls._try_parse_json(raw_text),
            field_name=f"script.{script_index}.parsed_json",
        )
        js_assignments = SafeField.get(
            lambda: cls._extract_js_assignments(raw_text),
            fallback=[],
            field_name=f"script.{script_index}.js_assignments",
        )

        dealership_hints = SafeField.get(
            lambda: cls._detect_dealership_hints(raw_text),
            fallback=[],
            field_name=f"script.{script_index}.dealership_hints",
        )

        return {
            "script_index": script_index,
            "script_id": SafeField.get(lambda: script.get("id"), field_name="script.id"),
            "script_type": SafeField.get(lambda: script.get("type"), field_name="script.type"),
            "script_src": SafeField.get(lambda: script.get("src"), field_name="script.src"),
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(script),
                fallback={},
                field_name="script.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(script),
                fallback={},
                field_name="script.data_attributes",
            ),
            "raw_text": raw_text,
            "raw_text_length": len(raw_text),
            "parsed_json": parsed_json,
            "js_assignments": js_assignments,
            "dealership_hints": dealership_hints,
        }

    @staticmethod
    def _try_parse_json(raw_text: str) -> Any | None:
        text = raw_text.strip()
        if not text:
            return None
        if not (text.startswith("{") or text.startswith("[")):
            return None
        return json.loads(text)

    @classmethod
    def _extract_js_assignments(cls, raw_text: str) -> list[dict[str, Any]]:
        assignments: list[dict[str, Any]] = []
        for match in JS_ASSIGNMENT_RE.finditer(raw_text):
            variable_name = match.group(1)
            raw_value = match.group(2)
            parsed_value = SafeField.get(
                lambda value=raw_value: cls._try_parse_json(value),
                field_name=f"js_assignment.{variable_name}",
            )
            assignments.append(
                {
                    "variable_name": variable_name,
                    "raw_value": raw_value,
                    "parsed_value": parsed_value,
                }
            )
        return assignments

    @staticmethod
    def _detect_dealership_hints(raw_text: str) -> list[str]:
        hints: list[str] = []
        keywords = (
            "dealer",
            "dealership",
            "agent",
            "branch",
            "نمایندگی",
            "نماینده",
            "tablepress",
            "location",
            "maps",
            "neshan",
            "balad",
        )
        lowered = raw_text.lower()
        for keyword in keywords:
            if keyword in lowered:
                hints.append(keyword)
        return hints


class DealershipRecordBuilder:
    """Build per-dealership records from the primary TablePress table."""

    PRIMARY_TABLE_ID = "tablepress-1"

    @classmethod
    def build_from_tables(cls, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        primary = next(
            (table for table in tables if table.get("table_id") == cls.PRIMARY_TABLE_ID),
            tables[0] if tables else None,
        )
        if primary is None:
            return []

        records: list[dict[str, Any]] = []
        for row in primary.get("rows", []):
            records.append(
                SafeField.get(
                    lambda r=row, table=primary: cls._build_record(table, r),
                    fallback={"error": "dealership_record_failed", "row": row},
                    field_name="dealership_record",
                )
            )
        return records

    @classmethod
    def _build_record(cls, table: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        columns_by_header: dict[str, Any] = {}
        for column in row.get("columns", []):
            header = column.get("header_label") or f"column_{column.get('column_index')}"
            columns_by_header[str(header)] = column.get("cell")

        return {
            "record_type": "active_dealership",
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "table_id": table.get("table_id"),
            "table_index": table.get("table_index"),
            "row_index": row.get("row_index"),
            "row_class": row.get("row_class"),
            "columns_by_header": columns_by_header,
            "columns": row.get("columns"),
            "links": row.get("links"),
            "images": row.get("images"),
            "iframes": row.get("iframes"),
            "row_attributes": row.get("row_attributes"),
            "row_data_attributes": row.get("row_data_attributes"),
            "html_row": row.get("html_row"),
        }


class LucanoDealershipCrawler:
    """Orchestrate fetch, multi-layer extraction, and JSON export."""

    def __init__(self, output_path: Path) -> None:
        self._http = HttpClient()
        self._output_path = output_path

    def crawl(self) -> dict[str, Any]:
        html, fetch_meta = self._fetch_html()
        soup = BeautifulSoup(html, "html.parser")

        tables = SafeField.get(
            lambda: DynamicStructureExtractor.extract_tables(soup),
            fallback=[],
            field_name="extract.tables",
        )
        div_blocks = SafeField.get(
            lambda: DynamicStructureExtractor.extract_div_blocks(soup),
            fallback=[],
            field_name="extract.div_blocks",
        )
        list_items = SafeField.get(
            lambda: DynamicStructureExtractor.extract_list_items(soup),
            fallback=[],
            field_name="extract.list_items",
        )
        hidden_scripts = SafeField.get(
            lambda: ScriptLayerScanner.scan(soup),
            fallback=[],
            field_name="extract.scripts",
        )
        dealerships = SafeField.get(
            lambda: DealershipRecordBuilder.build_from_tables(tables),
            fallback=[],
            field_name="extract.dealerships",
        )
        page_iframes = SafeField.get(
            lambda: HtmlNodeExtractor._extract_iframes(soup),
            fallback=[],
            field_name="extract.page_iframes",
        )
        page_metadata = SafeField.get(
            lambda: self._extract_page_metadata(soup, fetch_meta),
            fallback={},
            field_name="extract.page_metadata",
        )

        return {
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "fetch_meta": fetch_meta,
            "page_metadata": page_metadata,
            "total_dealerships": len(dealerships),
            "tables": tables,
            "div_blocks": div_blocks,
            "list_items": list_items,
            "page_iframes": page_iframes,
            "hidden_scripts": hidden_scripts,
            "dealerships": dealerships,
        }

    def _fetch_html(self) -> tuple[str, dict[str, Any]]:
        LOGGER.info("Fetching page: %s", TARGET_URL)
        try:
            return self._http.get_html(TARGET_URL)
        except SSLError as exc:
            raise RuntimeError(f"SSL error while fetching page: {exc}") from exc
        except (Timeout, ReadTimeout) as exc:
            raise RuntimeError(f"Timeout while fetching page: {exc}") from exc
        except ConnectionError as exc:
            raise RuntimeError(f"Connection error while fetching page: {exc}") from exc
        except RequestException as exc:
            raise RuntimeError(f"HTTP error while fetching page: {exc}") from exc

    @staticmethod
    def _extract_page_metadata(
        soup: BeautifulSoup,
        fetch_meta: dict[str, Any],
    ) -> dict[str, Any]:
        title_tag = soup.find("title")
        canonical = soup.find("link", rel="canonical")
        og_description = soup.find("meta", property="og:description")

        return {
            "title": SafeField.get(
                lambda: title_tag.get_text(strip=True) if title_tag else None,
                field_name="page.title",
            ),
            "canonical_url": SafeField.get(
                lambda: canonical.get("href") if canonical else None,
                field_name="page.canonical",
            ),
            "og_description": SafeField.get(
                lambda: og_description.get("content") if og_description else None,
                field_name="page.og_description",
            ),
            "final_url": fetch_meta.get("final_url"),
            "encoding": fetch_meta.get("encoding"),
            "content_length": fetch_meta.get("content_length"),
        }

    def save(self, payload: dict[str, Any]) -> Path:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        LOGGER.info(
            "Saved %d dealership records to %s",
            payload.get("total_dealerships", 0),
            self._output_path,
        )
        return self._output_path


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl Ecodalucano dealerships into raw JSON."
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

    crawler = LucanoDealershipCrawler(output_path=args.output)
    try:
        payload = crawler.crawl()
        output_file = crawler.save(payload)
    except Exception as exc:  # noqa: BLE001 — top-level guard for CLI
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    print(
        f"[+] Done: {payload['total_dealerships']} dealership records, "
        f"{len(payload.get('tables', []))} tables, "
        f"{len(payload.get('hidden_scripts', []))} scripts "
        f"-> {output_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
