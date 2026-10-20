"""Khodro45 magazine — WordPress REST category 176 (شرایط فروش)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from requests.exceptions import RequestException, Timeout

from src.scraper.sell_news.adapters.base import AdapterStats, SourceAdapter
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import DAILY_LIST_PAGES, DAILY_LOOKBACK_DAYS, SOURCE_KHODRO45, TEHRAN_TZ
from src.scraper.sell_news.normalize import lookback_cutoff, parse_jalali_datetime
from src.scraper.sell_news.records import NewsArticle

LOGGER = logging.getLogger(__name__)

API_BASE = "https://khodro45.com/mag/wp-json/wp/v2/posts"
LIST_URL = "https://khodro45.com/mag/category/news/sell-conditions/"
CATEGORY_ID = 176
PER_PAGE = 20


class Khodro45Adapter(SourceAdapter):
    source = SOURCE_KHODRO45
    list_url = LIST_URL

    def discover_and_fetch(
        self,
        *,
        backfill: bool = False,
        list_pages: int = DAILY_LIST_PAGES,
        lookback_days: int = DAILY_LOOKBACK_DAYS,
        limit: int | None = None,
    ) -> tuple[list[NewsArticle], AdapterStats]:
        stats = AdapterStats(source=self.source)
        cutoff = lookback_cutoff(lookback_days)
        posts: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        page = 1
        total_pages = 1
        while True:
            batch, total_pages, status = self._fetch_page(page)
            if status and status >= 400:
                stats.error += 1
                stats.notes.append(f"list page {page} http {status}")
                break
            if not batch:
                break
            for post in batch:
                pid = int(post.get("id") or 0)
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                posts.append(post)
            in_first_pages = page <= list_pages
            oldest = _post_published(batch[-1]) if batch else None
            page += 1
            self.http.sleep_polite()
            if backfill:
                if page > total_pages:
                    break
                continue
            if page > list_pages and (oldest is None or oldest < cutoff):
                break
            if not in_first_pages and oldest is not None and oldest < cutoff:
                break
            if page > total_pages:
                break

        # Daily: keep 7-day articles plus anything from the first N pages.
        selected: list[dict[str, Any]] = []
        for i, post in enumerate(posts):
            published = _post_published(post)
            from_first_pages = i < list_pages * PER_PAGE
            if backfill or from_first_pages or (published and published >= cutoff):
                selected.append(post)

        if limit is not None:
            selected = selected[:limit]
        stats.found = len(selected)

        articles: list[NewsArticle] = []
        for post in selected:
            try:
                article = self._post_to_article(post)
                articles.append(article)
                stats.fetched += 1
            except Exception as exc:
                stats.error += 1
                LOGGER.exception("khodro45 parse failed id=%s", post.get("id"))
                stats.notes.append(f"post {post.get('id')}: {type(exc).__name__}")
        return articles, stats

    def _fetch_page(self, page: int) -> tuple[list[dict[str, Any]], int, int | None]:
        try:
            response = self.http.get(
                API_BASE,
                params={
                    "categories": CATEGORY_ID,
                    "per_page": PER_PAGE,
                    "page": page,
                    "_embed": "1",
                },
            )
        except Timeout:
            LOGGER.warning("khodro45 timeout page=%s", page)
            return [], 1, None
        except RequestException as exc:
            LOGGER.warning("khodro45 list error page=%s %s", page, exc)
            return [], 1, None
        total_pages = int(response.headers.get("X-WP-TotalPages") or 1)
        if response.status_code >= 400:
            return [], total_pages, response.status_code
        data = response.json()
        if not isinstance(data, list):
            return [], total_pages, response.status_code
        return data, total_pages, response.status_code

    def _post_to_article(self, post: dict[str, Any]) -> NewsArticle:
        title = ((post.get("title") or {}).get("rendered")) if isinstance(post.get("title"), dict) else post.get("title")
        content = ""
        if isinstance(post.get("content"), dict):
            content = post["content"].get("rendered") or ""
        excerpt = ""
        if isinstance(post.get("excerpt"), dict):
            excerpt = post["excerpt"].get("rendered") or ""
        published = _post_published(post)
        modified = _parse_wp_date(post.get("modified_gmt") or post.get("modified"))
        image = _featured_image(post)
        return assemble_article(
            source=self.source,
            url=str(post.get("link") or ""),
            title=str(title or ""),
            html=content,
            published_at=published,
            source_list_url=self.list_url,
            source_article_id=str(post.get("id") or ""),
            modified_at=modified,
            image_url=image,
            subtitle=excerpt,
            http_status=200,
            raw_json={
                "wp_id": post.get("id"),
                "slug": post.get("slug"),
                "date": post.get("date"),
                "date_gmt": post.get("date_gmt"),
            },
        )


def _post_published(post: dict[str, Any]) -> datetime | None:
    return _parse_wp_date(post.get("date_gmt"), assume_utc=True) or _parse_wp_date(post.get("date"))


def _parse_wp_date(value: Any, *, assume_utc: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return parse_jalali_datetime(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if assume_utc else TEHRAN_TZ)
    return dt


def _featured_image(post: dict[str, Any]) -> str | None:
    embedded = post.get("_embedded") or {}
    media = embedded.get("wp:featuredmedia") or []
    if media and isinstance(media[0], dict):
        url = media[0].get("source_url")
        if url:
            return str(url)
    return None
