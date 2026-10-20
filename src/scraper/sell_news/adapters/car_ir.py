"""car.ir sell-news listing — Nuxt SSR HTML + paginated ?page=N."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from requests.exceptions import ConnectTimeout, ReadTimeout, RequestException, Timeout

from src.scraper.sell_news.adapters.base import AdapterStats, SourceAdapter
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import DAILY_LIST_PAGES, DAILY_LOOKBACK_DAYS, SOURCE_CAR_IR
from src.scraper.sell_news.normalize import canonical_url, lookback_cutoff, parse_jalali_datetime
from src.scraper.sell_news.records import NewsArticle

LOGGER = logging.getLogger(__name__)

LIST_URL = "https://car.ir/news/sell"
ARTICLE_HREF_RE = re.compile(r"/news/(\d+)-[^/?#]+", re.I)
HUB_HREF_RE = re.compile(r"/news/[a-z0-9-]+-car-sales/?$", re.I)


class CarIrAdapter(SourceAdapter):
    source = SOURCE_CAR_IR
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
        discovered: list[dict[str, str]] = []
        seen: set[str] = set()
        max_pages = 40 if backfill else max(list_pages, 1)
        last_page = max_pages
        for page in range(1, max_pages + 1):
            try:
                items, last_page = self._fetch_list_page(page)
            except (ConnectTimeout, ReadTimeout, Timeout) as exc:
                stats.timeout = True
                stats.error += 1
                stats.notes.append(f"list timeout page={page}: {type(exc).__name__}")
                LOGGER.warning("car.ir list timeout page=%s", page)
                break
            except RequestException as exc:
                stats.error += 1
                stats.notes.append(f"list error page={page}: {type(exc).__name__}: {exc}")
                LOGGER.warning("car.ir list error page=%s %s", page, exc)
                break
            new_on_page = 0
            for item in items:
                url = item["url"]
                if url in seen:
                    continue
                seen.add(url)
                discovered.append(item)
                new_on_page += 1
            self.http.sleep_polite()
            if not backfill and page >= list_pages:
                # Continue only while recent articles still appear.
                dates = [parse_jalali_datetime(it.get("published")) for it in items]
                dates = [d for d in dates if d is not None]
                if dates and max(dates) < cutoff and page >= list_pages:
                    break
            if page >= last_page or new_on_page == 0:
                break

        selected: list[dict[str, str]] = []
        for i, item in enumerate(discovered):
            published = parse_jalali_datetime(item.get("published"))
            from_first = i < 25 * list_pages
            if backfill or from_first or (published and published >= cutoff):
                selected.append(item)
        if limit is not None:
            selected = selected[:limit]
        stats.found = len(selected)

        articles: list[NewsArticle] = []
        for item in selected:
            try:
                article = self._fetch_article(item)
                if article is None:
                    stats.skipped_non_article += 1
                    continue
                articles.append(article)
                stats.fetched += 1
            except (ConnectTimeout, ReadTimeout, Timeout):
                stats.timeout = True
                stats.error += 1
                stats.notes.append(f"article timeout {item['url']}")
            except RequestException as exc:
                stats.error += 1
                stats.notes.append(f"article error {item['url']}: {type(exc).__name__}")
                LOGGER.warning("car.ir article error %s %s", item["url"], exc)
            except Exception:
                stats.error += 1
                LOGGER.exception("car.ir parse failed %s", item.get("url"))
            self.http.sleep_polite()
        return articles, stats

    def _fetch_list_page(self, page: int) -> tuple[list[dict[str, str]], int]:
        url = LIST_URL if page <= 1 else f"{LIST_URL}?page={page}"
        response = self.http.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        last_page = _nuxt_last_page(soup) or page
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if HUB_HREF_RE.search(href) or href.rstrip("/") == "/news/sell":
                continue
            m = ARTICLE_HREF_RE.search(href)
            if not m:
                continue
            full = canonical_url(urljoin("https://car.ir", href))
            if full in seen:
                continue
            seen.add(full)
            text = " ".join(a.get_text(" ", strip=True).split())
            items.append(
                {
                    "url": full,
                    "id": m.group(1),
                    "title": text,
                    "published": text,
                }
            )
        return items, last_page

    def _fetch_article(self, item: dict[str, str]) -> NewsArticle | None:
        response = self.http.get(item["url"])
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else item.get("title")
        published, image, content_html = _nuxt_article_fields(soup)
        if published is None:
            published = parse_jalali_datetime(item.get("published"))
        body_html = content_html or _article_body_html(soup) or html
        if not title:
            return None
        return assemble_article(
            source=self.source,
            url=item["url"],
            title=title,
            html=body_html,
            published_at=published,
            source_list_url=self.list_url,
            source_article_id=item.get("id"),
            image_url=image,
            http_status=response.status_code,
            raw_json={"list_title": item.get("title")},
        )


def _nuxt_last_page(soup: BeautifulSoup) -> int | None:
    script = soup.find("script", id="__NUXT_DATA__")
    if not script or not script.string:
        return None
    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for el in data:
        if isinstance(el, dict) and "lastPage" in el:
            value = el["lastPage"]
            if isinstance(value, int) and value > 20:
                # pointer into payload
                pointed = data[value] if value < len(data) else value
                if isinstance(pointed, int) and pointed < 10000:
                    return int(pointed)
            if isinstance(value, int) and value < 10000:
                return value
    return None


def _nuxt_article_fields(soup: BeautifulSoup) -> tuple:
    script = soup.find("script", id="__NUXT_DATA__")
    if not script or not script.string:
        return None, None, None
    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(data, list):
        return None, None, None

    def resolve(idx: object) -> object:
        if isinstance(idx, int) and 0 <= idx < len(data):
            val = data[idx]
            if isinstance(val, (str, int, float, bool)) or val is None:
                return val
        return idx

    published = None
    image = None
    content_html = None
    for el in data:
        if not isinstance(el, dict) or "content" not in el or "date" not in el:
            continue
        published = parse_jalali_datetime(str(resolve(el.get("date")) or ""))
        image_val = resolve(el.get("image"))
        if isinstance(image_val, str) and image_val.startswith("http"):
            image = image_val
        content_val = resolve(el.get("content"))
        if isinstance(content_val, str) and ("<table" in content_val.lower() or len(content_val) > 80):
            content_html = content_val
        break
    return published, image, content_html


def _article_body_html(soup: BeautifulSoup) -> str | None:
    for sel in ("article", ".page-news-show", ".news"):
        el = soup.select_one(sel)
        if isinstance(el, Tag) and el.find("table"):
            return str(el)
    tables = soup.find_all("table")
    if tables:
        return "".join(str(t) for t in tables)
    return None
