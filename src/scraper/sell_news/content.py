"""DOM-order article body → content_blocks (text + cars; no images)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from src.scraper.sell_news.constants import (
    CONTENT_BLOCKS_VERSION,
    SOURCE_AUTONOVIN,
    SOURCE_CAR_IR,
    SOURCE_KHODRO45,
    SOURCE_KHODROBANK,
    SOURCE_MEGFA,
)
from src.scraper.sell_news.normalize import article_text, first_article_sentences, html_fragment_text
from src.scraper.sell_news.parse import car_row_key, extract_cars_from_html, extract_cars_from_table, repair_broken_table_html
from src.scraper.sell_news.records import CarRow

TEXT_BLOCK_TYPES = frozenset({"heading", "paragraph", "list", "quote", "note"})
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "form",
        "svg",
        "button",
        "input",
        "select",
        "textarea",
        "img",
        "picture",
        "source",
        "video",
        "audio",
        "canvas",
        "link",
        "meta",
    }
)
NOISE_SELECTORS = (
    "header",
    "footer",
    "nav",
    "aside",
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    ".share",
    ".social-share",
    ".sharedaddy",
    ".post-share",
    ".share-box",
    ".advertisement",
    ".adsbygoogle",
    ".ad-box",
    ".ads-box",
    ".sidebar",
    ".widget",
    ".related-posts",
    ".related-news",
    ".related-articles",
    ".comments",
    ".comment-respond",
    "#comments",
    ".tags",
    ".post-tags",
    ".tagcloud",
    ".hot-topics",
    ".trending",
    ".breadcrumb",
    ".breadcrumbs",
    ".login-box",
    ".register-box",
    ".k45-cta-top",
    ".k45-cta-bottom",
    ".site-header",
    ".site-footer",
    ".page-header",
    ".page-footer",
)
NOISE_CLASS_ID_RE = re.compile(
    r"(share|social[-_]?share|advert|adsbygoogle|sidebar|related[-_]?(post|news|article)"
    r"|comment|breadcrumb|newsletter|login|register|hot[-_]?topic|trending|footer|header-nav)",
    re.I,
)
ALWAYS_NOISE_ANYWHERE = (
    "موضوعات داغ",
    "ورود | ثبت نام",
    "ورود | ثبت‌نام",
    "ورود|ثبت نام",
    "Google Tag Manager",
    "جستجوهای محبوب",
)
NOISE_TEXT_HINTS = (
    "موضوعات داغ",
    "ورود | ثبت نام",
    "ورود | ثبت‌نام",
    "ورود|ثبت نام",
    "اخبار مرتبط",
    "مطالب مرتبط",
    "نظرات کاربران",
    "ارسال نظر",
    "اشتراک گذاری",
    "اشتراک‌گذاری",
    "پیشنهاد سردبیر",
    "دنبال کنید",
    "Google Tag Manager",
    "جستجوهای محبوب",
    "فهرست محتوا",
    "بیشتر بخوانید",
)
CHROME_MARKERS = (
    "google tag manager",
    "جستجوهای محبوب",
    "k45-search",
    "gtm.js",
)
NOTE_CLASS_RE = re.compile(
    r"(alert|warning|notice|callout|notif|info-box|note-box|پیام|هشدار|توجه)",
    re.I,
)
SOURCE_BODY_SELECTORS: dict[str, tuple[str, ...]] = {
    SOURCE_KHODRO45: (
        ".k45-content",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".single-content",
        ".single-post-content",
    ),
    SOURCE_CAR_IR: (
        ".page-news-show .typography",
        ".typography",
        ".news-content",
        ".article-content",
        ".post-content",
    ),
    SOURCE_KHODROBANK: (
        ".news-single_content_txt",
        ".news-single_content",
        ".article-content",
        ".post-content",
    ),
    SOURCE_MEGFA: (
        ".announcement",
        ".article-content",
        ".post-content",
    ),
    SOURCE_AUTONOVIN: (
        ".announcement",
        ".article-content",
        ".post-content",
    ),
}
GENERIC_BODY_SELECTORS = (
    ".article-content",
    ".post-content",
    ".entry-content",
    ".news-single_content_txt",
    ".news-single_content",
    ".single-content",
)


@dataclass
class StructuredBody:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    cars: list[CarRow] = field(default_factory=list)
    full_text: str | None = None
    version: int = CONTENT_BLOCKS_VERSION


def extract_structured_body(
    html: str | None,
    *,
    title: str | None = None,
    source: str | None = None,
    cars: list[CarRow] | None = None,
) -> StructuredBody:
    """Walk the article body in DOM order. Does not emit image blocks."""

    out = StructuredBody()
    if cars is None:
        cars, _method = extract_cars_from_html(html, title=title)
    out.cars = cars
    if not html:
        if cars:
            indexes = [c.row_index for c in cars]
            out.blocks = [{"type": "cars", "row_indexes": indexes}]
        return out

    repaired = repair_broken_table_html(html)
    soup = BeautifulSoup(repaired, "html.parser")
    body = locate_article_root(soup, source)
    if body is None:
        if cars:
            out.blocks = [{"type": "cars", "row_indexes": [c.row_index for c in cars]}]
        return out
    strip_noise(body)

    events: list[tuple[str, Any]] = []
    _walk(body, events, title=title or "")
    used_indexes: set[int] = set()

    blocks: list[dict[str, Any]] = []
    for kind, payload in events:
        if kind == "table":
            table_rows, _used = extract_cars_from_table(payload, title=title)
            indexes = _match_row_indexes(table_rows, cars, used_indexes)
            if indexes:
                blocks.append({"type": "cars", "row_indexes": indexes})
            continue
        if kind == "block" and isinstance(payload, dict):
            if _is_valid_text_block(payload):
                blocks.append(payload)

    leftover = [c.row_index for c in cars if c.row_index not in used_indexes]
    if leftover:
        blocks.append({"type": "cars", "row_indexes": leftover})

    out.blocks = _dedupe_adjacent_empty(blocks)
    out.full_text = blocks_to_full_text(out.blocks)
    return out


def locate_article_root(soup: BeautifulSoup, source: str | None) -> Tag | None:
    selectors = SOURCE_BODY_SELECTORS.get(source or "", ()) + GENERIC_BODY_SELECTORS
    for sel in selectors:
        el = soup.select_one(sel)
        if isinstance(el, Tag) and _root_has_content(el) and not _looks_like_chrome(el):
            return el
    if soup.body is None and _root_has_content(soup) and not _looks_like_chrome(soup):
        return soup
    article = soup.select_one("article")
    if isinstance(article, Tag) and _root_has_content(article) and not _looks_like_chrome(article):
        return article
    return None


def is_usable_article_html(html: str | None) -> bool:
    """True for stored article fragments, not a full site chrome page."""

    if not html or len(html.strip()) < 40:
        return False
    head = html.lstrip()[:400].lower()
    if "<!doctype html" in head or head.startswith("<html"):
        return False
    sample = html[:8000]
    lowered = sample.lower()
    if "google tag manager" in lowered or "جستجوهای محبوب" in sample:
        return False
    return True


def extract_body_html(html: str | None, source: str | None = None) -> str | None:
    if not html:
        return None
    repaired = repair_broken_table_html(html)
    soup = BeautifulSoup(repaired, "html.parser")
    body = locate_article_root(soup, source)
    if body is None:
        return None
    strip_noise(body)
    return str(body)


def strip_noise(root: Tag) -> None:
    for sel in NOISE_SELECTORS:
        for el in list(root.select(sel)):
            if isinstance(el, Tag) and el is not root:
                el.decompose()
    for el in list(root.find_all(True)):
        if not isinstance(el, Tag) or el is root:
            continue
        if el.parent is None or not isinstance(getattr(el, "attrs", None), dict):
            continue
        classes = el.get("class") or []
        if isinstance(classes, str):
            class_blob = classes
        else:
            class_blob = " ".join(str(c) for c in classes)
        blob = f"{class_blob} {el.get('id') or ''}"
        if NOISE_CLASS_ID_RE.search(blob):
            el.decompose()


def blocks_to_full_text(blocks: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind not in TEXT_BLOCK_TYPES:
            continue
        if kind == "list":
            items = [article_text(x) for x in (block.get("items") or [])]
            items = [x for x in items if x]
            if not items:
                continue
            if block.get("ordered"):
                rendered = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
            else:
                rendered = "\n".join(f"• {item}" for item in items)
            parts.append(rendered)
            continue
        text = article_text(block.get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts) if parts else None


def subtitle_for_article(
    source_subtitle: str | None,
    full_text: str | None,
    *,
    previous_full_text: str | None = None,
) -> str | None:
    dek = html_fragment_text(source_subtitle) if source_subtitle else None
    if dek and _is_noise_text(dek):
        dek = None
    if dek and previous_full_text and dek == article_text(previous_full_text):
        dek = None
    if dek and full_text and dek == full_text:
        dek = None
    if dek and full_text and len(dek) >= max(len(full_text) - 8, 1) and dek in full_text.replace("\n", " "):
        if len(full_text) < 240:
            dek = None
    if dek:
        return dek
    lead = first_article_sentences(full_text, max_sentences=2)
    if lead and _is_noise_text(lead):
        lead = None
    if not lead or not full_text:
        return None
    compact_body = article_text(full_text.replace("\n", " ")) or ""
    if lead == compact_body or lead == full_text:
        return None
    if len(lead) >= max(int(0.9 * len(compact_body)), 1):
        return None
    return lead


def has_valid_text_block(blocks: list[dict[str, Any]] | None) -> bool:
    return any(_is_valid_text_block(b) for b in (blocks or []) if b.get("type") in TEXT_BLOCK_TYPES)


def _is_valid_text_block(block: dict[str, Any]) -> bool:
    kind = block.get("type")
    if kind == "list":
        items = [article_text(x) for x in (block.get("items") or [])]
        return any(items)
    if kind in TEXT_BLOCK_TYPES:
        return bool(article_text(block.get("text")))
    return False


def _root_has_content(el: Tag) -> bool:
    if el.find(["p", "h2", "h3", "h4", "table", "ul", "ol", "blockquote"]):
        return True
    text = article_text(el.get_text(" ", strip=True))
    return bool(text and len(text) > 40)


def _looks_like_chrome(el: Tag) -> bool:
    html = str(el)[:4000]
    text = el.get_text(" ", strip=True)[:800] if hasattr(el, "get_text") else ""
    blob = f"{html} {text}".lower()
    return any(marker in blob or marker in text for marker in CHROME_MARKERS)


def _walk(el: Tag, events: list[tuple[str, Any]], *, title: str) -> None:
    for child in list(el.children):
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            text = article_text(str(child))
            if text and not _is_noise_text(text):
                events.append(("block", {"type": "paragraph", "text": text}))
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in SKIP_TAGS:
            continue
        if name == "figure":
            _walk_figure(child, events)
            continue
        if name in HEADING_TAGS:
            text = _element_text(child)
            if text and text != title and not _is_noise_text(text):
                level = int(name[1])
                if level < 2:
                    level = 2
                if level > 6:
                    level = 6
                events.append(("block", {"type": "heading", "level": level, "text": text}))
            continue
        if name == "p":
            if _is_note_box(child) and not child.find("table"):
                text = _element_text(child)
                if text and not _is_noise_text(text):
                    events.append(("block", {"type": "note", "text": text}))
                continue
            text = _element_text(child)
            if text and not _is_noise_text(text):
                events.append(("block", {"type": "paragraph", "text": text}))
            continue
        if name in {"ul", "ol"}:
            items = _list_items(child)
            if items:
                events.append(
                    (
                        "block",
                        {"type": "list", "ordered": name == "ol", "items": items},
                    )
                )
            continue
        if name == "blockquote":
            text = _element_text(child)
            if text and not _is_noise_text(text):
                events.append(("block", {"type": "quote", "text": text}))
            continue
        if name == "table":
            events.append(("table", child))
            continue
        if _is_note_box(child) and not child.find("table") and len(child.find_all("p")) <= 3:
            text = _element_text(child)
            if text and not _is_noise_text(text):
                events.append(("block", {"type": "note", "text": text}))
            continue
        _walk(child, events, title=title)


def _walk_figure(el: Tag, events: list[tuple[str, Any]]) -> None:
    seen: set[int] = set()
    for table in el.find_all("table"):
        if isinstance(table, Tag) and id(table) not in seen:
            seen.add(id(table))
            events.append(("table", table))
    caption = el.find("figcaption")
    if isinstance(caption, Tag):
        text = _element_text(caption)
        if text and not _is_noise_text(text):
            events.append(("block", {"type": "paragraph", "text": text}))


def _element_text(el: Tag) -> str | None:
    work = BeautifulSoup(str(el), "html.parser")
    root = work.find(True)
    if not isinstance(root, Tag):
        return article_text(str(el))
    for nested in root.find_all(["table", "ul", "ol", "script", "style", "img", "figure"]):
        nested.decompose()
    for br in root.find_all("br"):
        br.replace_with(" ")
    return article_text(root.get_text(" ", strip=True))


def _list_items(el: Tag) -> list[str]:
    items: list[str] = []
    for li in el.find_all("li", recursive=False):
        if not isinstance(li, Tag):
            continue
        text = article_text(li.get_text(" ", strip=True))
        if text and not _is_noise_text(text):
            items.append(text)
    if items:
        return items
    for li in el.find_all("li"):
        if not isinstance(li, Tag):
            continue
        text = article_text(li.get_text(" ", strip=True))
        if text and not _is_noise_text(text):
            items.append(text)
    return items


def _is_note_box(el: Tag) -> bool:
    blob = " ".join(el.get("class") or []) + " " + str(el.get("id") or "")
    return bool(NOTE_CLASS_RE.search(blob))


def _is_noise_text(text: str) -> bool:
    compact = article_text(text) or ""
    if not compact:
        return True
    if compact.startswith("<") and ">" in compact[:120]:
        return True
    lowered = compact.lower()
    if any(hint in compact or hint.lower() in lowered for hint in ALWAYS_NOISE_ANYWHERE):
        return True
    for hint in NOISE_TEXT_HINTS:
        if compact.startswith(hint):
            return True
        if hint in compact[:80]:
            return True
        if hint in compact and len(compact) < 160:
            return True
    return False


def _match_row_indexes(
    table_rows: list[CarRow],
    all_cars: list[CarRow],
    used_indexes: set[int],
) -> list[int]:
    indexes: list[int] = []
    for parsed in table_rows:
        key = car_row_key(parsed)
        for car in all_cars:
            if car.row_index in used_indexes:
                continue
            if car_row_key(car) == key:
                indexes.append(car.row_index)
                used_indexes.add(car.row_index)
                break
    return indexes


def _dedupe_adjacent_empty(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "cars":
            indexes = [int(i) for i in (block.get("row_indexes") or []) if isinstance(i, int) or str(i).isdigit()]
            indexes = [int(i) for i in indexes]
            if not indexes:
                continue
            block = {"type": "cars", "row_indexes": indexes}
        elif kind in TEXT_BLOCK_TYPES and not _is_valid_text_block(block):
            continue
        if out and out[-1] == block:
            continue
        out.append(block)
    return out


def absolute_url(src: str | None, base: str | None) -> str | None:
    """Kept for callers; images are not stored in content_blocks."""

    if not src:
        return None
    text = src.strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if base:
        text = urljoin(base, text)
    if text.startswith("http://"):
        text = "https://" + text[len("http://") :]
    if not text.startswith("https://"):
        return None
    return text
