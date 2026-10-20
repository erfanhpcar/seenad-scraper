"""Parse car technical specifications from Bama.ir car-reviews spec pages (SSR DOM).

Each spec page is one trim. Sibling trims are SSR links under ``.chips a.chip``;
clicking a chip navigates to a distinct URL and the whole spec block changes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

LOGGER = logging.getLogger(__name__)

BAMA_DOMAIN = "https://bama.ir"
REQUEST_TIMEOUT_S = 45.0

SPEC_PATH_RE = re.compile(
    r"^/car-reviews/(?P<brand_slug>[a-z0-9-]+)/"
    r"(?P<model_slug>[a-z0-9.-]+)-specs-(?P<model_id>\d+)"
    r"(?:-(?P<trim_slug>[a-z0-9.-]+))?$",
    re.IGNORECASE,
)
_SCORE_PAIR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*/\s*10\s+(.+?)(?=\s+\d+(?:\.\d+)?\s*/\s*10\s+|\s+نقاط\s|$)",
)

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


def parse_spec_identity(url: str) -> dict[str, str]:
    """Split a car-reviews spec URL into brand / model / model_id / trim slug."""
    path = urlparse(url).path.rstrip("/") or "/"
    match = SPEC_PATH_RE.fullmatch(path)
    if match is None:
        return {
            "brand_slug": "",
            "model_slug": "",
            "model_id": "",
            "trim_slug": "",
        }
    groups = match.groupdict()
    return {
        "brand_slug": (groups.get("brand_slug") or "").lower(),
        "model_slug": (groups.get("model_slug") or "").lower(),
        "model_id": groups.get("model_id") or "",
        "trim_slug": (groups.get("trim_slug") or "").lower(),
    }


def absolute_spec_url(href: str) -> str | None:
    raw = (href or "").strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:")):
        return None
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        if parsed.netloc and "bama.ir" not in parsed.netloc.lower():
            return None
        path = parsed.path or ""
    else:
        path = raw.split("?")[0].split("#")[0]
        if not path.startswith("/"):
            path = f"/{path}"
    path = path.rstrip("/") or path
    if not SPEC_PATH_RE.fullmatch(path):
        return None
    return urljoin(BAMA_DOMAIN, path)


class BamaSpecsParser:
    """Fetch and parse technical spec sections from a Bama car-reviews page."""

    def __init__(self, url: str) -> None:
        normalized = url.strip()
        if not normalized.startswith("http"):
            normalized = f"{BAMA_DOMAIN}/{normalized.lstrip('/')}"
        self.url = normalized

    def fetch_html(self) -> str:
        """Download page HTML with browser-like headers."""
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

    def parse_page(self, html_content: str) -> dict[str, Any]:
        """Parse identity, sibling trims, review extras, and spec sections."""
        soup = BeautifulSoup(html_content, "html.parser")
        identity = parse_spec_identity(self.url)
        title, year_label = self._parse_heading(soup)
        trims = self._parse_trim_chips(soup)
        active = next((item for item in trims if item.get("is_active")), None)
        specs = self.parse_specs(html_content)
        review = self._parse_review(soup)
        return {
            "url": self.url,
            "brand_slug": identity["brand_slug"],
            "model_slug": identity["model_slug"],
            "model_id": identity["model_id"],
            "trim_slug": identity["trim_slug"] or (active or {}).get("trim_slug", ""),
            "trim_name": (active or {}).get("trim_name", ""),
            "title": title,
            "year_label": year_label,
            "trims": trims,
            "review": review,
            "specs": specs,
            "section_count": len(specs),
            "item_count": sum(len(section) for section in specs.values()),
        }

    def _parse_heading(self, soup: BeautifulSoup) -> tuple[str, str]:
        heading = soup.select_one("h1.car-reviews-base__title")
        if heading is None:
            heading = soup.select_one("h1")
        if heading is None:
            return "", ""
        spans = [el for el in heading.find_all("span", recursive=False) if _normalize_text(el.get_text())]
        if len(spans) >= 2:
            title = _normalize_text(spans[0].get_text())
            subtitle = _normalize_text(spans[1].get_text())
            year_label = subtitle
            for prefix in ("سال ", "سال"):
                if prefix in subtitle:
                    year_label = subtitle.split(prefix, 1)[-1].strip()
                    break
            return title, year_label
        return _normalize_text(heading.get_text()), ""

    def _parse_trim_chips(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        chips: list[dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in soup.select(".chips a.chip[href]"):
            abs_url = absolute_spec_url(str(anchor.get("href") or ""))
            if not abs_url or abs_url in seen:
                continue
            seen.add(abs_url)
            identity = parse_spec_identity(abs_url)
            classes = anchor.get("class") or []
            chips.append(
                {
                    "url": abs_url,
                    "trim_slug": identity["trim_slug"],
                    "trim_name": _normalize_text(anchor.get_text()),
                    "is_active": "active" in classes or anchor.get("aria-current") == "page",
                }
            )
        return chips

    @staticmethod
    def _parse_review(soup: BeautifulSoup) -> dict[str, Any]:
        review: dict[str, Any] = {
            "final_score": None,
            "scores": {},
            "strengths": [],
            "weaknesses": [],
        }
        score_el = soup.select_one(".final-score .score")
        if score_el is not None:
            nums = re.findall(r"\d+(?:\.\d+)?", _normalize_text(score_el.get_text()))
            if len(nums) >= 2:
                review["final_score"] = float(nums[-1])
            elif nums:
                review["final_score"] = float(nums[0])

        holder = soup.select_one(".details-holder")
        if holder is not None:
            blob = _normalize_text(holder.get_text(" "))
            start = blob.find("امتیازهای پنج‌گانه")
            if start != -1:
                chunk = blob[start + len("امتیازهای پنج‌گانه") :]
                stop = chunk.find("نقاط ")
                if stop != -1:
                    chunk = chunk[:stop]
                for value, label in _SCORE_PAIR_RE.findall(chunk):
                    review["scores"][_normalize_text(label)] = float(value)

        for column in soup.select(".cons-pros-column"):
            title_el = column.select_one(".cons-pros-title")
            title = _normalize_text(title_el.get_text()) if title_el else ""
            items = [
                _normalize_text(span.get_text())
                for span in column.select(".cons-pros > span")
                if _normalize_text(span.get_text())
            ]
            if "قوت" in title:
                review["strengths"] = items
            elif "ضعف" in title:
                review["weaknesses"] = items
        return review

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
    example_url = "https://bama.ir/car-reviews/mvm/x22-specs-1081-atbasic"
    parser = BamaSpecsParser(example_url)
    html = parser.fetch_html()
    page = parser.parse_page(html)
    print(json.dumps(page, ensure_ascii=False, indent=2))
