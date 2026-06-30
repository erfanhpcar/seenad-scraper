"""Parse car technical specifications from Bama.ir car-reviews spec pages (SSR DOM)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

LOGGER = logging.getLogger(__name__)

BAMA_DOMAIN = "https://bama.ir"
REQUEST_TIMEOUT_S = 20.0

DEFAULT_HEADERS = {
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

_FA_AR_TO_EN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
# RTL / bidi control marks only — keep ZWNJ (\u200c) for correct Persian orthography.
_INVISIBLE_CHARS_RE = re.compile(r"[\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """Strip invisible RTL marks, collapse whitespace, normalize Persian/Arabic digits."""
    cleaned = _INVISIBLE_CHARS_RE.sub("", text)
    cleaned = cleaned.translate(_FA_AR_TO_EN_DIGITS)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _referer_for_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return f"{BAMA_DOMAIN}/"


class BamaSpecsParser:
    """Fetch and parse technical spec sections from a Bama car-reviews page."""

    def __init__(self, url: str) -> None:
        normalized = url.strip()
        if not normalized.startswith("http"):
            normalized = f"{BAMA_DOMAIN}/{normalized.lstrip('/')}"
        self.url = normalized

    def fetch_html(self) -> str:
        """Download page HTML with browser-like headers and a 20s timeout."""
        headers = dict(DEFAULT_HEADERS)
        headers["Referer"] = _referer_for_url(self.url)

        LOGGER.debug("Fetching Bama specs page: %s", self.url)
        with httpx.Client(
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            response = client.get(self.url)

        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"Bama specs fetch failed with status {response.status_code}",
                request=response.request,
                response=response,
            )

        encoding = response.encoding or "utf-8"
        html = response.text
        if not html.lstrip().startswith(("<!DOCTYPE", "<html", "<HTML")):
            html = response.content.decode(encoding, errors="replace")

        LOGGER.debug(
            "Fetched Bama specs page status=%s bytes=%s url=%s",
            response.status_code,
            len(response.content),
            response.url,
        )
        return html

    def parse_specs(self, html_content: str) -> dict[str, dict[str, Any]]:
        """
        Parse SSR DOM spec sections into a nested dictionary.

        Returns ``{section_title: {feature_name: value}}`` where values are
        ``str`` for text specs or ``bool`` for yes/no feature flags.
        """
        soup = BeautifulSoup(html_content, "html.parser")
        sections: dict[str, dict[str, Any]] = {}

        for header in soup.select(".spec-header"):
            title_el = header.select_one(".spec-header__title")
            if title_el is None:
                continue

            section_title = _normalize_text(title_el.get_text())
            if not section_title:
                continue

            container = header.parent
            if container is None:
                continue

            section_items: dict[str, Any] = {}
            for spec_item in container.select(".spec-item"):
                key_el = spec_item.select_one(".spec-item__title")
                if key_el is None:
                    continue

                key = _normalize_text(key_el.get_text())
                if not key:
                    continue

                value = self._parse_spec_item_value(spec_item)
                section_items[key] = value

            if section_items:
                sections[section_title] = section_items

        return sections

    @staticmethod
    def _parse_spec_item_value(spec_item: Tag) -> Any:
        value_el = spec_item.select_one(".spec-item__value")
        if value_el is not None:
            return _normalize_text(value_el.get_text())

        svg = spec_item.select_one("svg")
        if svg is not None:
            classes = svg.get("class") or []
            if "check-icon" in classes:
                return True
            if "close-icon" in classes:
                return False

        raw = _normalize_text(spec_item.get_text())
        if raw.lower() in {"true", "false"}:
            return raw.lower() == "true"
        return raw


if __name__ == "__main__":
    example_url = "https://bama.ir/car-reviews/arya/at-specs-1642-2.0liter"
    parser = BamaSpecsParser(example_url)
    html = parser.fetch_html()
    specs = parser.parse_specs(html)
    print(json.dumps(specs, ensure_ascii=False, indent=2))
