from __future__ import annotations

from dataclasses import dataclass, field

from src.scraper.sell_news.http import SellNewsHttp
from src.scraper.sell_news.records import NewsArticle


@dataclass
class AdapterStats:
    source: str
    found: int = 0
    fetched: int = 0
    error: int = 0
    skipped_non_article: int = 0
    timeout: bool = False
    notes: list[str] = field(default_factory=list)

    def as_log_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "found": self.found,
            "fetched": self.fetched,
            "error": self.error,
            "skipped_non_article": self.skipped_non_article,
            "timeout": self.timeout,
            "notes": self.notes,
        }


class SourceAdapter:
    source: str
    list_url: str

    def __init__(self, http: SellNewsHttp) -> None:
        self.http = http

    def discover_and_fetch(
        self,
        *,
        backfill: bool = False,
        list_pages: int = 3,
        lookback_days: int = 7,
        limit: int | None = None,
    ) -> tuple[list[NewsArticle], AdapterStats]:
        raise NotImplementedError
