#!/usr/bin/env python3
"""
Daily crawler: sell-condition / بخشنامه news → Postgres.

Sources: khodro45 (WP REST cat 176), khodrobank (category 14 + RSS), car.ir (/news/sell),
autonovin (order API saleDetails), megfa (salecars.ir/announcements).
Default daily mode: last 7 days + first 3 list pages. Use --backfill for archive.

Usage:
  python scripts/crawl_sell_news.py --dry-run --samples 2 -v
  python scripts/crawl_sell_news.py --limit 20
  python scripts/crawl_sell_news.py --source khodro45 --source khodrobank
  python scripts/crawl_sell_news.py --backfill --source khodro45
  python scripts/backfill_sell_news_content_blocks.py --dry-run --limit 5 -v
  docker exec -i seenad_backend python3 scripts/crawl_sell_news.py --dry-run --samples 2

Cron: 11:00 Tehran (must finish by 11:10 for Seenad 11:15) and 16:00 Tehran.
Prefer the sequential morning window (USD then this job):

  CRON_TZ=Asia/Tehran
  0 11 * * * docker exec seenad_backend python3 scripts/run_seenad_1115_window.py \\
      >> /var/www/seenad-scraper/data/seenad_1115_window.log 2>&1
  0 16 * * * docker exec seenad_backend python3 scripts/crawl_sell_news.py \\
      >> /var/www/seenad-scraper/data/sell_news_cron.log 2>&1
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.sell_news.run import main

if __name__ == "__main__":
    raise SystemExit(main())
