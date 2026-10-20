#!/usr/bin/env python3
"""
Backfill structured content_blocks for existing car_sell_news rows.

Re-fetches article HTML (falls back to stored full_html), runs the block
extractor, and updates content_blocks / full_text / subtitle. Does not wipe
full_text on fetch/parse failure. Car rows are replaced only when the new
table parse is strictly more complete.

Usage:
  python scripts/backfill_sell_news_content_blocks.py --dry-run --limit 5 -v
  python scripts/backfill_sell_news_content_blocks.py --source khodro45
  python scripts/backfill_sell_news_content_blocks.py --force --id 12 --id 15
  python scripts/backfill_sell_news_content_blocks.py --resume
  docker exec -i seenad_backend python3 scripts/backfill_sell_news_content_blocks.py -v
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import CarSellNews
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import SOURCES
from src.scraper.sell_news.content import extract_body_html, is_usable_article_html
from src.scraper.sell_news.http import SellNewsHttp
from src.scraper.sell_news.records import CarRow
from src.scraper.sell_news.store import apply_content_backfill, cars_are_better

LOGGER = logging.getLogger("sell-news-content-backfill")
SAMPLES_PATH = Path("data/sell_news_content_blocks_samples.json")


@dataclass
class BackfillReport:
    updated: int = 0
    update_cars: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    updated_ids: list[int] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _select_ids(
    *,
    source: str | None,
    ids: list[int] | None,
    after_id: int | None,
    limit: int | None,
    force: bool,
) -> list[int]:
    stmt = (
        select(CarSellNews.id)
        .where(CarSellNews.parse_ok.is_(True))
        .where(CarSellNews.canonical_url.is_not(None))
        .order_by(CarSellNews.id.asc())
    )
    if source:
        stmt = stmt.where(CarSellNews.source == source)
    if ids:
        stmt = stmt.where(CarSellNews.id.in_(ids))
    if after_id:
        stmt = stmt.where(CarSellNews.id > after_id)
    if not force:
        stmt = stmt.where(CarSellNews.content_blocks.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    with SessionLocal() as session:
        return list(session.scalars(stmt).all())


def _fetch_html(http: SellNewsHttp, snapshot: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    try:
        response = http.get(snapshot["canonical_url"])
        status = response.status_code
        if status >= 400:
            return None, status, f"http {status}"
        return response.text, status, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _html_for_parse(fetched: str | None, snapshot: dict[str, Any]) -> str | None:
    stored = snapshot.get("full_html")
    live_body = extract_body_html(fetched, snapshot["source"]) if fetched else None
    if is_usable_article_html(live_body):
        return live_body
    if is_usable_article_html(stored):
        return stored
    return live_body or stored or fetched


def backfill_content_blocks(
    *,
    source: str | None = None,
    ids: list[int] | None = None,
    after_id: int | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    write_samples: bool = False,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    http = SellNewsHttp()
    report = BackfillReport()
    samples: dict[str, dict[str, Any]] = {}
    news_ids = _select_ids(source=source, ids=ids, after_id=after_id, limit=limit, force=force)
    LOGGER.info("backfill start rows=%s force=%s dry_run=%s", len(news_ids), force, dry_run)

    last_host = None
    for news_id in news_ids:
        with SessionLocal() as session:
            row = session.get(CarSellNews, news_id, options=[selectinload(CarSellNews.cars)])
            if row is None:
                report.failed += 1
                continue
            if row.content_blocks and not force:
                report.skipped += 1
                continue
            snapshot = {
                "id": row.id,
                "source": row.source,
                "canonical_url": row.canonical_url,
                "title": row.title,
                "subtitle": row.subtitle,
                "full_html": row.full_html,
                "full_text": row.full_text,
                "published_at": row.published_at,
                "source_list_url": row.source_list_url,
                "source_article_id": row.source_article_id,
                "modified_at": row.modified_at,
                "image_url": row.image_url,
                "cars": [
                    CarRow(
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
                        extra_json=car.extra_json or {},
                        extract_method=car.extract_method,
                    )
                    for car in (row.cars or [])
                ],
            }

        host = (snapshot["canonical_url"] or "").split("/")[2] if snapshot["canonical_url"] else ""
        if last_host is not None:
            http.sleep_polite()
        last_host = host

        fetched, status, fetch_err = _fetch_html(http, snapshot)
        try:
            html = _html_for_parse(fetched, snapshot)
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"id={snapshot['id']} body {type(exc).__name__}: {exc}")
            report.failed_urls.append(snapshot["canonical_url"])
            LOGGER.exception("backfill body extract failed id=%s", snapshot["id"])
            continue
        if not html:
            report.failed += 1
            msg = fetch_err or "no html"
            report.errors.append(f"id={snapshot['id']} {snapshot['canonical_url']}: {msg}")
            report.failed_urls.append(snapshot["canonical_url"])
            LOGGER.warning(
                "backfill failed id=%s url=%s err=%s",
                snapshot["id"],
                snapshot["canonical_url"],
                msg,
            )
            continue

        try:
            article = assemble_article(
                source=snapshot["source"],
                url=snapshot["canonical_url"],
                title=snapshot["title"],
                html=html,
                published_at=snapshot["published_at"],
                source_list_url=snapshot["source_list_url"],
                source_article_id=snapshot["source_article_id"],
                modified_at=snapshot["modified_at"],
                image_url=snapshot["image_url"],
                subtitle=snapshot["subtitle"],
                http_status=status,
                previous_full_text=snapshot["full_text"],
            )
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"id={snapshot['id']} parse {type(exc).__name__}: {exc}")
            report.failed_urls.append(snapshot["canonical_url"])
            LOGGER.exception(
                "backfill parse failed id=%s url=%s",
                snapshot["id"],
                snapshot["canonical_url"],
            )
            continue

        if not article.content_blocks and not article.cars:
            report.failed += 1
            report.errors.append(f"id={snapshot['id']} empty blocks")
            report.failed_urls.append(snapshot["canonical_url"])
            LOGGER.warning("backfill empty id=%s url=%s", snapshot["id"], snapshot["canonical_url"])
            continue

        if write_samples and snapshot["source"] not in samples:
            samples[snapshot["source"]] = {
                "id": snapshot["id"],
                "source": snapshot["source"],
                "canonical_url": snapshot["canonical_url"],
                "title": article.title,
                "subtitle": article.subtitle,
                "full_text": article.full_text,
                "content_blocks": article.content_blocks,
                "car_row_indexes": [c.row_index for c in article.cars],
            }

        replace_cars = cars_are_better(article.cars, snapshot["cars"])
        if dry_run:
            report.updated += 1
            if replace_cars:
                report.update_cars += 1
            report.updated_ids.append(snapshot["id"])
            LOGGER.info(
                "dry-run id=%s source=%s blocks=%s replace_cars=%s",
                snapshot["id"],
                snapshot["source"],
                len(article.content_blocks or []),
                replace_cars,
            )
            continue

        try:
            with SessionLocal.begin() as session:
                fresh = session.get(CarSellNews, snapshot["id"])
                if fresh is None:
                    report.failed += 1
                    continue
                action = apply_content_backfill(
                    session,
                    fresh,
                    article,
                    replace_cars=replace_cars,
                )
            report.updated += 1
            report.updated_ids.append(snapshot["id"])
            if action == "update_cars":
                report.update_cars += 1
            LOGGER.info(
                "updated id=%s source=%s blocks=%s cars_replaced=%s",
                snapshot["id"],
                snapshot["source"],
                len(article.content_blocks or []),
                replace_cars,
            )
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"id={snapshot['id']} upsert {type(exc).__name__}: {exc}")
            LOGGER.exception("backfill upsert failed id=%s", snapshot["id"])

    finished = datetime.now(timezone.utc)
    if write_samples and samples:
        SAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAMPLES_PATH.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("wrote samples %s sources=%s", SAMPLES_PATH, list(samples))

    return {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_s": round((finished - started).total_seconds(), 3),
        "dry_run": dry_run,
        "force": force,
        "totals": report.as_dict(),
        "samples_path": str(SAMPLES_PATH) if write_samples else None,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=list(SOURCES))
    parser.add_argument("--id", action="append", dest="ids", type=int)
    parser.add_argument("--after-id", type=int, default=None, help="Resume after this car_sell_news.id")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Re-parse rows that already have content_blocks.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write-samples", action="store_true", help="Write one JSON sample per source.")
    parser.add_argument("--resume", action="store_true", help="Skip rows that already have content_blocks (default).")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    summary = backfill_content_blocks(
        source=args.source,
        ids=args.ids,
        after_id=args.after_id,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        write_samples=args.write_samples,
    )
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    totals = summary.get("totals") or {}
    if totals.get("updated", 0) == 0 and totals.get("failed", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
