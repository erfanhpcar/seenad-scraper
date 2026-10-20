"""Idempotent Postgres upsert for car_sell_news + car_sell_news_cars."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import CarSellNews, CarSellNewsCar
from src.scraper.sell_news.content import is_usable_article_html
from src.scraper.sell_news.records import CarRow, NewsArticle

LOGGER = logging.getLogger(__name__)


@dataclass
class UpsertCounts:
    insert: int = 0
    update: int = 0
    skip: int = 0
    error: int = 0
    table_parsed: int = 0
    prose_fallback: int = 0

    def add_extract(self, method: str) -> None:
        if method == "prose_fallback":
            self.prose_fallback += 1
        elif method in {"html_table", "key_value_table"}:
            self.table_parsed += 1


def upsert_article(
    session: Session,
    article: NewsArticle,
    *,
    scrape_batch_id: str,
    crawled_at: datetime | None = None,
) -> str:
    """Insert or update one article and replace car rows when content changed.

    Returns 'insert' | 'update' | 'skip'.
    """

    crawled_at = crawled_at or datetime.now(timezone.utc)
    article.finalize_parse_ok()
    existing = session.scalar(
        select(CarSellNews).where(CarSellNews.canonical_url == article.canonical_url)
    )
    payload = _news_values(article, scrape_batch_id=scrape_batch_id, crawled_at=crawled_at)
    if existing is None:
        row = CarSellNews(**payload)
        session.add(row)
        session.flush()
        _replace_cars(session, row.id, article.cars)
        return "insert"

    changed = _content_changed(existing, article)
    for key, value in payload.items():
        setattr(existing, key, value)
    session.flush()
    if changed:
        _replace_cars(session, existing.id, article.cars)
        return "update"
    return "skip"


def persist_articles(
    articles: list[NewsArticle],
    *,
    scrape_batch_id: str,
    dry_run: bool = False,
) -> UpsertCounts:
    counts = UpsertCounts()
    if dry_run:
        for article in articles:
            counts.insert += 1
            if article.cars:
                counts.add_extract(article.cars[0].extract_method)
        return counts

    crawled_at = datetime.now(timezone.utc)
    for article in articles:
        try:
            with SessionLocal.begin() as session:
                action = upsert_article(
                    session,
                    article,
                    scrape_batch_id=scrape_batch_id,
                    crawled_at=crawled_at,
                )
            if action == "insert":
                counts.insert += 1
            elif action == "update":
                counts.update += 1
            else:
                counts.skip += 1
            if article.cars:
                counts.add_extract(article.cars[0].extract_method)
        except Exception:
            counts.error += 1
            LOGGER.exception("upsert failed url=%s", article.canonical_url)
    return counts


CAR_FILL_FIELDS = (
    "brand_raw",
    "model_raw",
    "trim_raw",
    "colors_raw",
    "price_raw",
    "price_toman",
    "plan_name_raw",
    "delivery_raw",
)


def cars_are_better(new_cars: list[CarRow], old_cars: list[Any]) -> bool:
    """True when the new table parse is strictly more complete than stored rows."""

    if not new_cars:
        return False
    if not old_cars:
        return True
    if len(new_cars) > len(old_cars):
        return True
    if len(new_cars) < len(old_cars):
        return False
    return _car_fill_score(new_cars) > _car_fill_score(old_cars)


def apply_content_backfill(
    session: Session,
    existing: CarSellNews,
    article: NewsArticle,
    *,
    replace_cars: bool,
) -> str:
    """Update structured body fields without changing canonical_url or scrape_batch_id."""

    now = datetime.now(timezone.utc)
    if article.full_text:
        existing.full_text = article.full_text
    existing.content_blocks = article.content_blocks
    existing.content_blocks_version = article.content_blocks_version or 1
    existing.subtitle = article.subtitle
    existing.parse_ok = article.parse_ok
    existing.parse_error = article.parse_error
    if article.http_status:
        existing.http_status = article.http_status
    if article.full_html and is_usable_article_html(article.full_html):
        existing.full_html = article.full_html
    existing.updated_at = now
    existing.crawled_at = now
    if replace_cars:
        _replace_cars(session, existing.id, article.cars)
        return "update_cars"
    return "update"


def _car_fill_score(cars: list[Any]) -> int:
    score = 0
    for car in cars:
        for name in CAR_FILL_FIELDS:
            value = getattr(car, name, None)
            if value not in (None, "", []):
                score += 1
    return score


def _content_changed(existing: CarSellNews, article: NewsArticle) -> bool:
    old_title = existing.title or ""
    new_title = article.title or ""
    old_text = existing.full_text or ""
    new_text = article.full_text or ""
    old_pub = existing.published_at.isoformat() if existing.published_at else ""
    new_pub = article.published_at.isoformat() if article.published_at else ""
    old_blocks = existing.content_blocks
    new_blocks = article.content_blocks
    return (old_title, old_text, old_pub, old_blocks) != (new_title, new_text, new_pub, new_blocks)


def _replace_cars(session: Session, news_id: int, cars: list[CarRow]) -> None:
    session.execute(delete(CarSellNewsCar).where(CarSellNewsCar.news_id == news_id))
    for car in cars:
        session.add(
            CarSellNewsCar(
                news_id=news_id,
                row_index=car.row_index,
                brand_raw=car.brand_raw,
                model_raw=car.model_raw,
                trim_raw=car.trim_raw,
                colors_raw=car.colors_raw,
                price_raw=car.price_raw,
                price_toman=car.price_toman,
                price_unit_guess=car.price_unit_guess,
                plan_name_raw=car.plan_name_raw,
                delivery_raw=car.delivery_raw,
                extra_json=car.extra_json or None,
                extract_method=car.extract_method,
            )
        )


def _news_values(
    article: NewsArticle,
    *,
    scrape_batch_id: str,
    crawled_at: datetime,
) -> dict[str, Any]:
    return {
        "source": article.source,
        "source_article_id": article.source_article_id,
        "canonical_url": article.canonical_url,
        "source_list_url": article.source_list_url,
        "category": article.category,
        "title": article.title,
        "subtitle": article.subtitle,
        "full_text": article.full_text,
        "full_html": article.full_html,
        "published_at": article.published_at,
        "modified_at": article.modified_at,
        "image_url": article.image_url,
        "company_name_raw": article.company_name_raw,
        "sale_type": article.sale_type,
        "sale_type_raw": article.sale_type_raw,
        "campaign_badge": article.campaign_badge,
        "matched_keywords": article.matched_keywords,
        "raw_json": article.raw_json,
        "parse_ok": article.parse_ok,
        "parse_error": article.parse_error,
        "http_status": article.http_status,
        "crawled_at": crawled_at,
        "scrape_batch_id": scrape_batch_id,
        "updated_at": crawled_at,
        "content_blocks": article.content_blocks,
        "content_blocks_version": article.content_blocks_version,
    }
