#!/usr/bin/env python3
"""
Probe live sell-condition sources and dump table schemas (no DB writes).

Usage:
  python scripts/probe_sell_news.py
  docker exec -i seenad_backend python3 scripts/probe_sell_news.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.sell_news.http import SellNewsHttp
from src.scraper.sell_news.normalize import html_to_text
from src.scraper.sell_news.parse import html_table_to_matrix, iter_tables, map_header
from src.scraper.sell_news.run import run_sell_news

OUTPUT = Path("data/sell_news_probe_sample.json")


def _schema_from_html(html: str | None) -> list[dict]:
    out = []
    for i, table in enumerate(iter_tables(html)):
        matrix = html_table_to_matrix(table)
        header = matrix[0] if matrix else []
        out.append(
            {
                "index": i,
                "rows": len(matrix),
                "header": header,
                "mapped": [map_header(c) for c in header],
                "sample_row": matrix[1] if len(matrix) > 1 else [],
            }
        )
    return out


def main() -> int:
    summary = run_sell_news(dry_run=True, samples=3, limit=9, list_pages=1)
    probe = {"run": {k: v for k, v in summary.items() if k != "samples"}, "articles": []}
    for source, samples in (summary.get("samples") or {}).items():
        for art in samples:
            probe["articles"].append(
                {
                    "source": source,
                    "title": art.get("title"),
                    "canonical_url": art.get("canonical_url"),
                    "published_at": art.get("published_at"),
                    "sale_type": art.get("sale_type"),
                    "cars": art.get("cars"),
                    "table_schemas": _schema_from_html(art.get("full_html")),
                    "text_preview": (html_to_text(art.get("full_html")) or "")[:280],
                }
            )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT} articles={len(probe['articles'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
