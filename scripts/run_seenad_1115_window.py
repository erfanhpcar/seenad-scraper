#!/usr/bin/env python3
"""
Seenad 11:15 pickup window — sequential morning jobs (Asia/Tehran).

Order:
  1) usd_irr_rate (fast)
  2) crawl_sell_news

Must finish by 11:10 so Seenad can SELECT at 11:15. Does not call any Seenad API.

Cron:
  CRON_TZ=Asia/Tehran
  0 11 * * * docker exec seenad_backend python3 scripts/run_seenad_1115_window.py \\
      >> /var/www/seenad-scraper/data/seenad_1115_window.log 2>&1

Use this host cron XOR Celery Beat, not both.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.sell_news.run import run_sell_news
from src.scraper.usd_irr_rate_service import run_usd_irr_rate_pipeline

LOGGER = logging.getLogger("seenad-1115-window")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    window_started = datetime.now(timezone.utc)
    LOGGER.info("seenad 11:15 window start started_at=%s", window_started.isoformat())

    usd = run_usd_irr_rate_pipeline(dry_run=False)
    LOGGER.info(
        "usd_irr_rate finished success=%s as_of_date=%s action=%s insert=%s update=%s "
        "carried_forward=%s elapsed_s=%s error=%s",
        usd.success,
        usd.as_of_date,
        usd.action,
        1 if usd.action == "insert" else 0,
        1 if usd.action == "update" else 0,
        usd.carried_forward,
        usd.elapsed_s,
        usd.error,
    )
    if not usd.success:
        LOGGER.error(
            "usd_irr_rate today's row NOT created as_of_date=%s — continuing to sell-news",
            usd.as_of_date,
        )

    news = run_sell_news(dry_run=False)
    totals = news.get("totals") or {}
    LOGGER.info(
        "sell-news finished batch=%s insert=%s update=%s skip=%s error=%s elapsed_s=%s",
        news.get("scrape_batch_id"),
        totals.get("insert"),
        totals.get("update"),
        totals.get("skip"),
        totals.get("error"),
        news.get("elapsed_s"),
    )

    window_finished = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "started_at": window_started.isoformat(),
        "finished_at": window_finished.isoformat(),
        "elapsed_s": round((window_finished - window_started).total_seconds(), 3),
        "usd_irr_rate": usd.as_celery_dict(),
        "sell_news": {
            "scrape_batch_id": news.get("scrape_batch_id"),
            "started_at": news.get("started_at"),
            "finished_at": news.get("finished_at"),
            "elapsed_s": news.get("elapsed_s"),
            "totals": totals,
            "sources": [
                {
                    "source": row.get("source"),
                    "found": row.get("found"),
                    "insert": row.get("insert"),
                    "update": row.get("update"),
                    "skip": row.get("skip"),
                    "error": row.get("error"),
                }
                for row in news.get("sources") or []
            ],
        },
    }
    LOGGER.info(
        "seenad 11:15 window done elapsed_s=%s usd_ok=%s news_errors=%s",
        payload["elapsed_s"],
        usd.success,
        totals.get("error"),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not usd.success and (totals.get("found") or 0) == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
