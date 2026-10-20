"""Khodrobank sell-conditions category (id 14) + RSS fallback."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException, Timeout

from src.scraper.sell_news.adapters.base import AdapterStats, SourceAdapter
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import (
    DAILY_LIST_PAGES,
    DAILY_LOOKBACK_DAYS,
    KHODROBANK_KEYWORDS,
    KHODROBANK_RSS_NOISE,
    KHODROBANK_RSS_STRONG,
    SOURCE_KHODROBANK,
)
from src.scraper.sell_news.normalize import (
    canonical_url,
    clean_text,
    lookback_cutoff,
    parse_jalali_datetime,
)
from src.scraper.sell_news.records import NewsArticle

LOGGER = logging.getLogger(__name__)

LIST_URL = (
    "https://www.khodrobank.com/"
    "%d8%a7%d8%ae%d8%a8%d8%a7%d8%b1-%d8%ae%d9%88%d8%af%d8%b1%d9%88/"
    "%d8%b4%d8%b1%d8%a7%db%8c%d8%b7-%d9%81%d8%b1%d9%88%d8%b4/14"
)
RSS_URL = "https://www.khodrobank.com/RSS/RSS.ashx"
REVIEW_RE = re.compile(r"/[Rr]eviews/(\d+)/")
HUB_HINTS = ("شرایط-فروش", "ثبت-نام", "ثبت_نام")


class KhodrobankAdapter(SourceAdapter):
    source = SOURCE_KHODROBANK
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
        discovered: dict[str, dict[str, str]] = {}

        max_pages = 40 if backfill else max(list_pages, 1)
        for page in range(1, max_pages + 1):
            try:
                items = self._fetch_list_page(page)
            except Timeout:
                stats.error += 1
                stats.notes.append(f"list timeout page={page}")
                break
            except RequestException as exc:
                stats.error += 1
                stats.notes.append(f"list error page={page}: {type(exc).__name__}")
                LOGGER.warning("khodrobank list error page=%s %s", page, exc)
                break
            new_count = 0
            oldest = None
            for item in items:
                url = item["url"]
                if url not in discovered:
                    discovered[url] = item
                    new_count += 1
                published = parse_jalali_datetime(item.get("published"))
                if published and (oldest is None or published < oldest):
                    oldest = published
            self.http.sleep_polite()
            if not items:
                break
            if not backfill and page >= list_pages and (oldest is None or oldest < cutoff):
                break

        rss_items, rss_notes = self._fetch_rss()
        stats.notes.extend(rss_notes)
        for item in rss_items:
            url = item["url"]
            if url not in discovered:
                discovered[url] = item

        selected: list[dict[str, str]] = []
        ordered = sorted(
            discovered.values(),
            key=lambda it: (0 if "/reviews/" in it["url"].lower() else 1, it.get("published") or ""),
            reverse=False,
        )
        for i, item in enumerate(ordered):
            published = parse_jalali_datetime(item.get("published"))
            from_first = i < 20 * list_pages
            if backfill or from_first or (published and published >= cutoff) or item.get("via") == "rss":
                if item.get("via") == "rss" and published and published < cutoff and not backfill:
                    continue
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
            except Timeout:
                stats.error += 1
                stats.notes.append(f"article timeout {item['url']}")
            except RequestException as exc:
                stats.error += 1
                stats.notes.append(f"article error {item['url']}: {type(exc).__name__}")
            except Exception:
                stats.error += 1
                LOGGER.exception("khodrobank parse failed %s", item.get("url"))
            self.http.sleep_polite()
        return articles, stats

    def _fetch_list_page(self, page: int) -> list[dict[str, str]]:
        params = {"page": page} if page > 1 else None
        response = self.http.get(self.list_url, params=params)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items: list[dict[str, str]] = []
        for block in soup.select(".kb-itemnews, .news_item"):
            if not isinstance(block, Tag):
                continue
            a = block.find("a", href=True)
            if not isinstance(a, Tag):
                continue
            href = str(a.get("href") or "")
            full = canonical_url(urljoin("https://www.khodrobank.com", href))
            if _is_noise_url(full):
                continue
            h2 = block.find("h2")
            title = h2.get_text(" ", strip=True) if h2 else block.get_text(" ", strip=True)
            date_el = block.select_one(".icon-calendar-b, .news_details")
            published = date_el.get_text(" ", strip=True) if date_el else ""
            review_id = None
            m = REVIEW_RE.search(full)
            if m:
                review_id = m.group(1)
            items.append(
                {
                    "url": full,
                    "title": title,
                    "published": published,
                    "id": review_id or "",
                    "via": "list",
                }
            )
        return items

    def _fetch_rss(self) -> tuple[list[dict[str, str]], list[str]]:
        notes: list[str] = []
        try:
            response = self.http.get(RSS_URL)
            response.raise_for_status()
        except Exception as exc:
            notes.append(f"rss failed: {type(exc).__name__}")
            return [], notes
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            notes.append("rss xml parse error")
            return [], notes
        kept: list[dict[str, str]] = []
        noise_titles: list[str] = []
        for node in root.findall(".//item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            desc = (node.findtext("description") or "").strip()
            pub = (node.findtext("pubDate") or "").strip()
            if not link:
                continue
            matched = [k for k in KHODROBANK_KEYWORDS if k in title or k in desc]
            if not matched:
                continue
            if _rss_is_noise(title, desc, matched):
                noise_titles.append(title[:80])
                continue
            m = REVIEW_RE.search(link)
            kept.append(
                {
                    "url": canonical_url(link),
                    "title": title,
                    "published": pub,
                    "id": m.group(1) if m else "",
                    "via": "rss",
                    "keywords": ",".join(matched),
                }
            )
        if noise_titles:
            notes.append("rss noise skipped: " + " | ".join(noise_titles[:8]))
        notes.append(f"rss kept {len(kept)} after keyword filter")
        return kept, notes

    def _fetch_article(self, item: dict[str, str]) -> NewsArticle | None:
        response = self.http.get(item["url"])
        soup = BeautifulSoup(response.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else item.get("title")
        time_el = soup.find("time")
        published = parse_jalali_datetime(time_el.get_text(" ", strip=True) if time_el else None)
        if published is None:
            published = parse_jalali_datetime(item.get("published"))
        body = soup.select_one(".news-single_content") or soup.select_one(
            ".news-single_content_txt"
        )
        html = str(body) if isinstance(body, Tag) else response.text
        if "<table" not in html.lower() and "<table" in response.text.lower():
            html = response.text
        image = None
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            image = str(og.get("content"))
        keywords = [k for k in (item.get("keywords") or "").split(",") if k]
        if not title:
            return None
        return assemble_article(
            source=self.source,
            url=response.url or item["url"],
            title=title,
            html=html,
            published_at=published,
            source_list_url=self.list_url,
            source_article_id=item.get("id") or None,
            image_url=image,
            http_status=response.status_code,
            matched_keywords=keywords or None,
            raw_json={"via": item.get("via"), "list_title": item.get("title")},
        )


def _is_noise_url(url: str) -> bool:
    lowered = url.lower()
    return "/cars/" in lowered or "/reviews/2312/" in lowered or "/reviews/14072/" in lowered


def _rss_is_noise(title: str, desc: str, matched: list[str]) -> bool:
    blob = f"{title} {desc}"
    if any(n in blob for n in KHODROBANK_RSS_NOISE):
        return True
    strong = any(s in blob for s in KHODROBANK_RSS_STRONG)
    if matched == ["فروش"] and not strong:
        return True
    return not strong
