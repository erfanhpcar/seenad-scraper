"""Daily orchestration for sell-news crawlers."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.scraper.sell_news.adapters.autonovin import AutonovinAdapter
from src.scraper.sell_news.adapters.car_ir import CarIrAdapter
from src.scraper.sell_news.adapters.khodro45 import Khodro45Adapter
from src.scraper.sell_news.adapters.khodrobank import KhodrobankAdapter
from src.scraper.sell_news.adapters.megfa import MegfaAdapter
from src.scraper.sell_news.constants import DAILY_LIST_PAGES, DAILY_LOOKBACK_DAYS, SOURCES
from src.scraper.sell_news.http import SellNewsHttp
from src.scraper.sell_news.normalize import jalali_batch_id, json_safe, tehran_now
from src.scraper.sell_news.records import NewsArticle
from src.scraper.sell_news.store import persist_articles

LOGGER = logging.getLogger("sell-news")

ADAPTERS = {
    "khodro45": Khodro45Adapter,
    "khodrobank": KhodrobankAdapter,
    "car.ir": CarIrAdapter,
    "autonovin": AutonovinAdapter,
    "megfa": MegfaAdapter,
}


@dataclass
class SourceReport:
    source: str
    found: int = 0
    fetched: int = 0
    insert: int = 0
    update: int = 0
    skip: int = 0
    error: int = 0
    table_parsed: int = 0
    prose_fallback: int = 0
    timeout: bool = False
    notes: list[str] = field(default_factory=list)
    coverage: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_sell_news(
    *,
    sources: list[str] | None = None,
    backfill: bool = False,
    list_pages: int = DAILY_LIST_PAGES,
    lookback_days: int = DAILY_LOOKBACK_DAYS,
    limit: int | None = None,
    dry_run: bool = False,
    samples: int = 0,
    scrape_batch_id: str | None = None,
) -> dict[str, Any]:
    batch_id = scrape_batch_id or jalali_batch_id()
    started = datetime.now(timezone.utc)
    LOGGER.info(
        "sell-news start batch=%s dry_run=%s backfill=%s started_at=%s",
        batch_id,
        dry_run,
        backfill,
        started.isoformat(),
    )
    selected = sources or list(SOURCES)
    http = SellNewsHttp()
    reports: list[SourceReport] = []
    sample_payload: dict[str, list[dict[str, Any]]] = {}
    all_articles: list[NewsArticle] = []

    for name in selected:
        cls = ADAPTERS.get(name)
        if cls is None:
            LOGGER.warning("unknown source %s", name)
            continue
        adapter = cls(http)
        LOGGER.info("source=%s start backfill=%s limit=%s", name, backfill, limit)
        articles, stats = adapter.discover_and_fetch(
            backfill=backfill,
            list_pages=list_pages,
            lookback_days=lookback_days,
            limit=limit,
        )
        counts = persist_articles(articles, scrape_batch_id=batch_id, dry_run=dry_run)
        report = SourceReport(
            source=name,
            found=stats.found,
            fetched=stats.fetched,
            insert=counts.insert,
            update=counts.update,
            skip=counts.skip,
            error=stats.error + counts.error,
            table_parsed=counts.table_parsed,
            prose_fallback=counts.prose_fallback,
            timeout=stats.timeout,
            notes=stats.notes,
            coverage=_coverage(articles),
        )
        reports.append(report)
        all_articles.extend(articles)
        if samples > 0:
            sample_payload[name] = [a.as_sample_dict() for a in articles[:samples]]
        LOGGER.info(
            "source=%s found=%s insert=%s update=%s skip=%s table_parsed=%s prose_fallback=%s error=%s",
            name,
            report.found,
            report.insert,
            report.update,
            report.skip,
            report.table_parsed,
            report.prose_fallback,
            report.error,
        )

    finished = datetime.now(timezone.utc)
    elapsed_s = round((finished - started).total_seconds(), 3)
    totals = {
        "found": sum(r.found for r in reports),
        "insert": sum(r.insert for r in reports),
        "update": sum(r.update for r in reports),
        "skip": sum(r.skip for r in reports),
        "error": sum(r.error for r in reports),
        "table_parsed": sum(r.table_parsed for r in reports),
        "prose_fallback": sum(r.prose_fallback for r in reports),
    }
    LOGGER.info(
        "sell-news done batch=%s started_at=%s finished_at=%s elapsed_s=%s "
        "insert=%s update=%s skip=%s error=%s",
        batch_id,
        started.isoformat(),
        finished.isoformat(),
        elapsed_s,
        totals["insert"],
        totals["update"],
        totals["skip"],
        totals["error"],
    )
    summary = {
        "scrape_batch_id": batch_id,
        "timezone": "Asia/Tehran",
        "ran_at": tehran_now().isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_s": elapsed_s,
        "dry_run": dry_run,
        "backfill": backfill,
        "sources": [r.as_dict() for r in reports],
        "totals": totals,
        "samples": json_safe(sample_payload) if samples else {},
        "utc": finished.isoformat(),
    }
    return summary


def _coverage(articles: list[NewsArticle]) -> dict[str, float]:
    cars = [c for a in articles for c in a.cars]
    if not cars:
        return {}
    fields = (
        "brand_raw",
        "model_raw",
        "trim_raw",
        "colors_raw",
        "price_raw",
        "price_toman",
        "plan_name_raw",
        "delivery_raw",
    )
    n = len(cars)
    out: dict[str, float] = {}
    for name in fields:
        filled = sum(1 for c in cars if getattr(c, name) not in (None, "", []))
        out[name] = round(100.0 * filled / n, 1)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily sell-condition / بخشنامه crawler (khodro45, khodrobank, car.ir, autonovin, megfa)."
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        choices=list(ADAPTERS),
        help="Repeat to choose sources (default: all).",
    )
    parser.add_argument("--backfill", action="store_true", help="Deep archive pagination.")
    parser.add_argument("--list-pages", type=int, default=DAILY_LIST_PAGES)
    parser.add_argument("--lookback-days", type=int, default=DAILY_LOOKBACK_DAYS)
    parser.add_argument("--limit", type=int, default=None, help="Max articles per source.")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write Postgres.")
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Print this many sample articles (JSON) per source.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    summary = run_sell_news(
        sources=args.sources,
        backfill=args.backfill,
        list_pages=args.list_pages,
        lookback_days=args.lookback_days,
        limit=args.limit,
        dry_run=args.dry_run,
        samples=args.samples if args.samples > 0 else (2 if args.dry_run else 0),
    )
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if summary["totals"]["found"] == 0 and summary["totals"]["error"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
