"""Megfa (salecars.ir) official imported-car sale announcements."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException, Timeout

from src.scraper.sell_news.adapters.base import AdapterStats, SourceAdapter
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import DAILY_LIST_PAGES, DAILY_LOOKBACK_DAYS, SOURCE_MEGFA
from src.scraper.sell_news.normalize import canonical_url, lookback_cutoff, parse_jalali_datetime
from src.scraper.sell_news.records import NewsArticle

LOGGER = logging.getLogger(__name__)

LIST_URL = "https://salecars.ir/announcements"
ANNOUNCEMENT_HREF_RE = re.compile(r"/announcement/(\d+)/?$", re.I)
JALALI_DATE_RE = re.compile(
    r"(?:13\d{2}|14\d{2})[/-]\d{1,2}[/-]\d{1,2}"
    r"|\d{1,2}\s+(?:فروردین|اردیبهشت|خرداد|تیر|مرداد|شهریور|مهر|آبان|آذر|دی|بهمن|اسفند)\s+(?:13\d{2}|14\d{2})"
)


class MegfaAdapter(SourceAdapter):
    source = SOURCE_MEGFA
    list_url = LIST_URL

    def discover_and_fetch(
        self,
        *,
        backfill: bool = False,
        list_pages: int = DAILY_LIST_PAGES,
        lookback_days: int = DAILY_LOOKBACK_DAYS,
        limit: int | None = None,
    ) -> tuple[list[NewsArticle], AdapterStats]:
        del list_pages  # single SSR list page; no pagination today
        stats = AdapterStats(source=self.source)
        cutoff = lookback_cutoff(lookback_days)
        try:
            items = self._fetch_list()
        except Timeout:
            stats.timeout = True
            stats.error += 1
            stats.notes.append("list timeout")
            return [], stats
        except RequestException as exc:
            stats.error += 1
            stats.notes.append(f"list error: {type(exc).__name__}: {exc}")
            LOGGER.warning("megfa list error %s", exc)
            return [], stats

        if limit is not None:
            items = items[:limit]
        stats.found = len(items)

        articles: list[NewsArticle] = []
        for item in items:
            try:
                article = self._fetch_article(item)
                if article is None:
                    stats.skipped_non_article += 1
                    continue
                published = article.published_at
                if not backfill and published is not None and published < cutoff:
                    stats.skipped_non_article += 1
                    continue
                articles.append(article)
                stats.fetched += 1
            except Timeout:
                stats.timeout = True
                stats.error += 1
                stats.notes.append(f"article timeout {item['url']}")
            except RequestException as exc:
                stats.error += 1
                stats.notes.append(f"article error {item['url']}: {type(exc).__name__}")
                LOGGER.warning("megfa article error %s %s", item["url"], exc)
            except Exception:
                stats.error += 1
                LOGGER.exception("megfa parse failed %s", item.get("url"))
            self.http.sleep_polite()
        return articles, stats

    def _fetch_list(self) -> list[dict[str, str]]:
        response = self.http.get(LIST_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "")
            match = ANNOUNCEMENT_HREF_RE.search(href)
            if not match:
                continue
            full = canonical_url(urljoin("https://salecars.ir", href))
            if full in seen:
                continue
            seen.add(full)
            card = a.find_parent("div", class_=re.compile(r"row"))
            title = _card_title(a, card)
            excerpt = ""
            if isinstance(card, Tag):
                paragraph = card.find("p")
                if isinstance(paragraph, Tag):
                    excerpt = paragraph.get_text(" ", strip=True)
            items.append(
                {
                    "url": full,
                    "id": match.group(1),
                    "title": title,
                    "excerpt": excerpt,
                }
            )
        return items

    def _fetch_article(self, item: dict[str, str]) -> NewsArticle | None:
        response = self.http.get(item["url"])
        soup = BeautifulSoup(response.text, "html.parser")
        body = soup.select_one(".announcement")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if h1_ok(heading) else item.get("title")
        html = str(body) if isinstance(body, Tag) else response.text
        published = first_jalali_datetime(title, item.get("excerpt"), _plain(html))
        image = None
        og = soup.find("meta", property="og:image")
        if isinstance(og, Tag) and og.get("content"):
            image = str(og.get("content"))
        if not title:
            return None
        return assemble_article(
            source=self.source,
            url=item["url"],
            title=title,
            html=html,
            published_at=published,
            source_list_url=self.list_url,
            source_article_id=item.get("id"),
            image_url=image,
            http_status=response.status_code,
            raw_json={"list_title": item.get("title"), "excerpt": item.get("excerpt")},
        )


def h1_ok(node: object) -> bool:
    return isinstance(node, Tag)


def _card_title(anchor: Tag, card: Tag | None) -> str:
    if isinstance(card, Tag):
        heading = card.find(["h5", "h4", "h3", "h2"])
        if isinstance(heading, Tag):
            text = heading.get_text(" ", strip=True)
            if text:
                return text
    img = anchor.find("img")
    if isinstance(img, Tag):
        alt = str(img.get("alt") or "").strip()
        if alt:
            return alt
    return anchor.get_text(" ", strip=True)


def _plain(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def first_jalali_datetime(*blobs: str | None):
    for blob in blobs:
        if not blob:
            continue
        match = JALALI_DATE_RE.search(blob)
        parsed = parse_jalali_datetime(match.group(0) if match else blob)
        if parsed is not None:
            return parsed
    return None
