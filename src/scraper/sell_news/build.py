"""Assemble a NewsArticle from fetched source fields + HTML body."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.scraper.sell_news.constants import CATEGORY_DEFAULT, CONTENT_BLOCKS_VERSION
from src.scraper.sell_news.content import extract_structured_body, subtitle_for_article
from src.scraper.sell_news.normalize import (
    canonical_url,
    classify_sale_type,
    decode_title,
    extract_company_name,
)
from src.scraper.sell_news.parse import extract_cars_from_html
from src.scraper.sell_news.records import NewsArticle


def assemble_article(
    *,
    source: str,
    url: str,
    title: str | None,
    html: str | None,
    published_at: datetime | None,
    source_list_url: str | None,
    source_article_id: str | None = None,
    modified_at: datetime | None = None,
    image_url: str | None = None,
    subtitle: str | None = None,
    http_status: int | None = None,
    matched_keywords: list[str] | None = None,
    raw_json: dict[str, Any] | None = None,
    parse_error: str | None = None,
    previous_full_text: str | None = None,
) -> NewsArticle:
    canon = canonical_url(url)
    clean_title = decode_title(title)
    full_html = html or None
    cars, _method = extract_cars_from_html(full_html, title=clean_title)
    structured = extract_structured_body(
        full_html,
        title=clean_title,
        source=source,
        cars=cars,
    )
    full_text = structured.full_text
    sub = subtitle_for_article(
        subtitle,
        full_text,
        previous_full_text=previous_full_text,
    )
    lead = " ".join(x for x in (clean_title, sub, (full_text or "")[:1800]) if x)
    sale_type, sale_raw = classify_sale_type(clean_title, lead)
    badge = None
    if sale_raw:
        badge = f"{sale_raw} فعال"
    article = NewsArticle(
        source=source,
        canonical_url=canon,
        title=clean_title or canon,
        source_article_id=source_article_id,
        source_list_url=source_list_url,
        category=CATEGORY_DEFAULT,
        subtitle=sub,
        full_text=full_text,
        full_html=full_html,
        published_at=published_at,
        modified_at=modified_at,
        image_url=image_url,
        company_name_raw=extract_company_name(clean_title, lead),
        sale_type=sale_type,
        sale_type_raw=sale_raw,
        campaign_badge=badge,
        matched_keywords=matched_keywords,
        raw_json=raw_json,
        parse_error=parse_error,
        http_status=http_status,
        cars=structured.cars,
        content_blocks=structured.blocks,
        content_blocks_version=CONTENT_BLOCKS_VERSION,
    )
    article.finalize_parse_ok()
    return article
