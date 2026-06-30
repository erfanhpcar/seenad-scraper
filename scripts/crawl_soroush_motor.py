#!/usr/bin/env python3
"""
Crawler: Soroush Motor (سروش موتور مبنا) passenger-car dealership map page.

Raw-data strategy: fetch the full HTML page, dynamically parse every table / div /
list / Elementor icon-list structure, scan all inline scripts for embedded JSON or
JS variables, and persist the untouched extraction output.

Usage:
  python scripts/crawl_soroush_motor.py
  python scripts/crawl_soroush_motor.py --output data/soroush_motor_raw.json -v

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

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError, Timeout

LOGGER = logging.getLogger("soroush-motor-crawler")

TARGET_URL = (
    "https://soroushmotor.com/"
    "%d8%b3%d8%b1%d9%88%d8%b4-%d9%85%d9%88%d8%aa%d9%88%d8%b1-%d9%85%d8%a8%d9%86%d8%a7_"
    "%d9%86%d9%85%d8%a7%db%8c%d9%86%d8%af%da%af%db%8c-%d8%a7%d8%b3%d8%aa%d8%a7%d9%86-%d9%87%d8%a7_%d8%b3%d9%88%d8%a7%d8%b1/"
)
DEFAULT_OUTPUT = Path("data/soroush_motor_raw.json")
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
    "location",
    "contact",
    "province",
    "agency",
    "نمایندگی",
    "نماینده",
    "استان",
    "card",
    "tablepress",
    "elementor-icon-list",
    "elementor-widget-icon-list",
    "jet-tabs",
    "wp-block",
    "map-marker",
    "soroush",
)

STRUCTURED_BLOCK_TAGS = ("div", "section", "article", "li")

JS_ASSIGNMENT_RE = re.compile(
    r"(?:var|const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*;",
    re.MULTILINE,
)
JSON_LD_TYPE = "application/ld+json"


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
                        lambda node=image: cls._extract_data_attributes(image),
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
    """Parse tables, div cards, list items, and Elementor icon lists without filtering."""

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
            if not cls._is_candidate_block(div):
                continue
            seen_ids.add(id(div))
            blocks.append(
                SafeField.get(
                    lambda node=div: cls._extract_block(node, "div"),
                    fallback={"error": "div_extraction_failed"},
                    field_name="div_block",
                )
            )
        return blocks

    @classmethod
    def extract_structured_blocks(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Extract div / section / article / li blocks matching dealership layout hints."""
        blocks: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for tag_name in STRUCTURED_BLOCK_TAGS:
            for node in soup.find_all(tag_name):
                if id(node) in seen_ids:
                    continue
                if not cls._is_candidate_block(node):
                    continue
                if not SafeField.get(
                    lambda n=node: n.get_text(" ", strip=True),
                    fallback="",
                    field_name="structured_block.text_check",
                ):
                    continue
                seen_ids.add(id(node))
                blocks.append(
                    SafeField.get(
                        lambda n=node, tn=tag_name: cls._extract_block(n, tn),
                        fallback={"error": "structured_block_extraction_failed"},
                        field_name=f"structured_block.{tag_name}",
                    )
                )
        return blocks

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
    def extract_icon_lists(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Capture Elementor icon-list widgets used for dealership contact cards."""
        icon_lists: list[dict[str, Any]] = []
        for list_index, icon_list in enumerate(
            soup.select(".elementor-icon-list-items, ul.elementor-icon-list-items"),
            start=1,
        ):
            items: list[dict[str, Any]] = []
            for item_index, item in enumerate(
                icon_list.select(".elementor-icon-list-item, li.elementor-icon-list-item"),
                start=1,
            ):
                items.append(
                    SafeField.get(
                        lambda node=item, li=item_index: cls._extract_icon_list_item(node, li),
                        fallback={"item_index": item_index, "error": "icon_list_item_failed"},
                        field_name=f"icon_list.{list_index}.item.{item_index}",
                    )
                )

            parent_widget = icon_list.find_parent(
                class_=lambda value: value and "elementor-widget" in " ".join(value)
            )
            icon_lists.append(
                {
                    "list_index": list_index,
                    "list_attributes": SafeField.get(
                        lambda node=icon_list: HtmlNodeExtractor._extract_attributes(node),
                        fallback={},
                        field_name="icon_list.attributes",
                    ),
                    "parent_widget": SafeField.get(
                        lambda node=parent_widget: HtmlNodeExtractor.extract_tag(node)
                        if isinstance(node, Tag)
                        else None,
                        field_name="icon_list.parent_widget",
                    ),
                    "items": items,
                    "html_list": SafeField.get(
                        lambda node=icon_list: HtmlNodeExtractor.extract_tag(node),
                        fallback={},
                        field_name="icon_list.html_list",
                    ),
                }
            )
        return icon_lists

    @classmethod
    def _extract_icon_list_item(cls, item: Tag, item_index: int) -> dict[str, Any]:
        text_nodes = item.select(".elementor-icon-list-text")
        icon_nodes = item.select(".elementor-icon-list-icon")
        return {
            "item_index": item_index,
            "text": SafeField.get(
                lambda: item.get_text("", strip=False) or None,
                field_name="icon_item.text",
            ),
            "text_stripped": SafeField.get(
                lambda: item.get_text(" ", strip=True) or None,
                field_name="icon_item.text_stripped",
            ),
            "icon_list_texts": SafeField.get(
                lambda: [node.get_text(" ", strip=True) for node in text_nodes],
                fallback=[],
                field_name="icon_item.icon_list_texts",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in item.contents) or None,
                field_name="icon_item.inner_html",
            ),
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(item),
                fallback={},
                field_name="icon_item.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(item),
                fallback={},
                field_name="icon_item.data_attributes",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(item),
                fallback=[],
                field_name="icon_item.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(item),
                fallback=[],
                field_name="icon_item.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(item),
                fallback=[],
                field_name="icon_item.iframes",
            ),
            "icon_nodes": SafeField.get(
                lambda: [
                    {
                        "class": " ".join(node.get("class", [])) or None,
                        "inner_html": "".join(str(child) for child in node.contents) or None,
                    }
                    for node in icon_nodes
                ],
                fallback=[],
                field_name="icon_item.icon_nodes",
            ),
            "html_item": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(item),
                fallback={},
                field_name="icon_item.html_item",
            ),
        }

    @staticmethod
    def _is_candidate_block(node: Tag) -> bool:
        class_text = " ".join(node.get("class", [])).lower()
        if any(hint in class_text for hint in DIV_CARD_HINTS):
            return True

        for attr_name in ("id", "data-widget_type", "data-element_type"):
            attr_value = str(node.get(attr_name, "")).lower()
            if any(hint in attr_value for hint in DIV_CARD_HINTS):
                return True

        data_settings = str(node.get("data-settings", "")).lower()
        return any(hint in data_settings for hint in DIV_CARD_HINTS)

    @classmethod
    def _extract_block(cls, node: Tag, block_type: str) -> dict[str, Any]:
        return {
            "block_type": block_type,
            "attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_attributes(node),
                fallback={},
                field_name=f"{block_type}.attributes",
            ),
            "data_attributes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_data_attributes(node),
                fallback={},
                field_name=f"{block_type}.data_attributes",
            ),
            "text": SafeField.get(
                lambda: node.get_text("", strip=False) or None,
                field_name=f"{block_type}.text",
            ),
            "text_stripped": SafeField.get(
                lambda: node.get_text(" ", strip=True) or None,
                field_name=f"{block_type}.text_stripped",
            ),
            "inner_html": SafeField.get(
                lambda: "".join(str(child) for child in node.contents) or None,
                field_name=f"{block_type}.inner_html",
            ),
            "links": SafeField.get(
                lambda: HtmlNodeExtractor._extract_links(node),
                fallback=[],
                field_name=f"{block_type}.links",
            ),
            "images": SafeField.get(
                lambda: HtmlNodeExtractor._extract_images(node),
                fallback=[],
                field_name=f"{block_type}.images",
            ),
            "iframes": SafeField.get(
                lambda: HtmlNodeExtractor._extract_iframes(node),
                fallback=[],
                field_name=f"{block_type}.iframes",
            ),
            "html_block": SafeField.get(
                lambda: HtmlNodeExtractor.extract_tag(node),
                fallback={},
                field_name=f"{block_type}.html_block",
            ),
        }

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
    """Inspect every script tag for JSON-LD, JSON payloads, or hard-coded JS variables."""

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
        script_type = SafeField.get(lambda: script.get("type"), field_name="script.type")

        parsed_json = SafeField.get(
            lambda: cls._try_parse_json(raw_text),
            field_name=f"script.{script_index}.parsed_json",
        )
        parsed_json_ld = SafeField.get(
            lambda: cls._try_parse_json_ld(script_type, raw_text),
            field_name=f"script.{script_index}.parsed_json_ld",
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
            "script_type": script_type,
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
            "parsed_json_ld": parsed_json_ld,
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

    @staticmethod
    def _try_parse_json_ld(script_type: str | None, raw_text: str) -> Any | None:
        if script_type != JSON_LD_TYPE:
            return None
        text = raw_text.strip()
        if not text:
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
            "province",
            "location",
            "نمایندگی",
            "نماینده",
            "استان",
            "tablepress",
            "maps",
            "neshan",
            "balad",
            "jet-tabs",
            "JetTabsSettings",
            "elementorFrontendConfig",
            "soroushmotor",
        )
        lowered = raw_text.lower()
        for keyword in keywords:
            if keyword.lower() in lowered:
                hints.append(keyword)
        return hints


class DealershipRecordBuilder:
    """Merge all extraction layers into unified dealership records."""

    @classmethod
    def build(
        cls,
        *,
        tables: list[dict[str, Any]],
        icon_lists: list[dict[str, Any]],
        structured_blocks: list[dict[str, Any]],
        list_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        records.extend(cls.build_from_tables(tables))
        records.extend(cls.build_from_icon_lists(icon_lists))
        records.extend(cls.build_from_structured_blocks(structured_blocks))
        records.extend(cls.build_from_list_items(list_items))
        return records

    @classmethod
    def build_from_tables(cls, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for table in tables:
            for row in table.get("rows", []):
                records.append(
                    SafeField.get(
                        lambda t=table, r=row: cls._build_table_record(t, r),
                        fallback={"record_type": "table_row", "error": "dealership_record_failed", "row": row},
                        field_name="dealership_record.table",
                    )
                )
        return records

    @classmethod
    def build_from_icon_lists(cls, icon_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for icon_list in icon_lists:
            for item in icon_list.get("items", []):
                records.append(
                    SafeField.get(
                        lambda lst=icon_list, it=item: cls._build_icon_list_record(lst, it),
                        fallback={"record_type": "icon_list_item", "error": "dealership_record_failed", "item": item},
                        field_name="dealership_record.icon_list",
                    )
                )
        return records

    @classmethod
    def build_from_structured_blocks(cls, structured_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for index, block in enumerate(structured_blocks, start=1):
            records.append(
                SafeField.get(
                    lambda b=block, i=index: cls._build_structured_block_record(b, i),
                    fallback={"record_type": "structured_block", "error": "dealership_record_failed", "block": block},
                    field_name="dealership_record.structured_block",
                )
            )
        return records

    @classmethod
    def build_from_list_items(cls, list_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in list_items:
            text = item.get("text_stripped") or ""
            if not text or len(text) > 500:
                continue
            if "menu-item" in str(item.get("attributes", {}).get("class", "")):
                continue
            records.append(
                SafeField.get(
                    lambda it=item: cls._build_list_item_record(it),
                    fallback={"record_type": "list_item", "error": "dealership_record_failed", "item": item},
                    field_name="dealership_record.list_item",
                )
            )
        return records

    @classmethod
    def _build_table_record(cls, table: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        columns_by_header: dict[str, Any] = {}
        for column in row.get("columns", []):
            header = column.get("header_label") or f"column_{column.get('column_index')}"
            columns_by_header[str(header)] = column.get("cell")

        return {
            "record_type": "table_row",
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

    @classmethod
    def _build_icon_list_record(cls, icon_list: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        return {
            "record_type": "icon_list_item",
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "list_index": icon_list.get("list_index"),
            "item_index": item.get("item_index"),
            "text": item.get("text"),
            "text_stripped": item.get("text_stripped"),
            "icon_list_texts": item.get("icon_list_texts"),
            "links": item.get("links"),
            "images": item.get("images"),
            "iframes": item.get("iframes"),
            "attributes": item.get("attributes"),
            "data_attributes": item.get("data_attributes"),
            "inner_html": item.get("inner_html"),
            "html_item": item.get("html_item"),
            "parent_widget": icon_list.get("parent_widget"),
        }

    @classmethod
    def _build_structured_block_record(cls, block: dict[str, Any], block_index: int) -> dict[str, Any]:
        return {
            "record_type": "structured_block",
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "block_index": block_index,
            "block_type": block.get("block_type"),
            "text": block.get("text"),
            "text_stripped": block.get("text_stripped"),
            "links": block.get("links"),
            "images": block.get("images"),
            "iframes": block.get("iframes"),
            "attributes": block.get("attributes"),
            "data_attributes": block.get("data_attributes"),
            "inner_html": block.get("inner_html"),
            "html_block": block.get("html_block"),
        }

    @classmethod
    def _build_list_item_record(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "record_type": "list_item",
            "source_url": TARGET_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "list_index": item.get("list_index"),
            "text": item.get("text"),
            "text_stripped": item.get("text_stripped"),
            "links": item.get("links"),
            "images": item.get("images"),
            "attributes": item.get("attributes"),
            "data_attributes": item.get("data_attributes"),
            "inner_html": item.get("inner_html"),
            "html_li": item.get("html_li"),
        }


class SoroushMotorCrawler:
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
        structured_blocks = SafeField.get(
            lambda: DynamicStructureExtractor.extract_structured_blocks(soup),
            fallback=[],
            field_name="extract.structured_blocks",
        )
        list_items = SafeField.get(
            lambda: DynamicStructureExtractor.extract_list_items(soup),
            fallback=[],
            field_name="extract.list_items",
        )
        icon_lists = SafeField.get(
            lambda: DynamicStructureExtractor.extract_icon_lists(soup),
            fallback=[],
            field_name="extract.icon_lists",
        )
        hidden_scripts = SafeField.get(
            lambda: ScriptLayerScanner.scan(soup),
            fallback=[],
            field_name="extract.scripts",
        )
        dealerships = SafeField.get(
            lambda: DealershipRecordBuilder.build(
                tables=tables,
                icon_lists=icon_lists,
                structured_blocks=structured_blocks,
                list_items=list_items,
            ),
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
            "structured_blocks": structured_blocks,
            "list_items": list_items,
            "icon_lists": icon_lists,
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
        description="Crawl Soroush Motor passenger dealerships into raw JSON."
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

    crawler = SoroushMotorCrawler(output_path=args.output)
    try:
        payload = crawler.crawl()
        output_file = crawler.save(payload)
    except Exception as exc:  # noqa: BLE001 — top-level guard for CLI
        LOGGER.error("Crawl failed: %s", exc)
        return 1

    print(
        f"[+] Done: {payload['total_dealerships']} dealership records, "
        f"{len(payload.get('tables', []))} tables, "
        f"{len(payload.get('icon_lists', []))} icon lists, "
        f"{len(payload.get('hidden_scripts', []))} scripts "
        f"-> {output_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
