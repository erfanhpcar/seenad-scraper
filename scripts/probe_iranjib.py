#!/usr/bin/env python3
"""
Probe: iranjib.ir zero-km market prices (domestic + imported catalogue pages).

Maps the two public SSR price tables:
  - /showgroup/45/قیمت-خودرو-تولید-داخل/  → domestic
  - /showgroup/46/قیمت-خودرو-وارداتی/     → imported

Findings this script re-validates:
  - Pages are server-rendered ``table.items_table`` (no JSON API)
  - Each data row has a chart link ``getchart.php?iid=N`` (stable row id)
  - Chart expand rows (``tr.downN.expand``) are not data rows
  - Headers label prices as تومان; sentinels include ناموجود / به زودی / ---

Usage:
  python scripts/probe_iranjib.py
  python scripts/probe_iranjib.py --kind domestic -v
  python scripts/probe_iranjib.py --limit-rows 8
  docker compose exec -T backend python3 scripts/probe_iranjib.py

Dependencies (project requirements.txt):
  requests, beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import RequestException, Timeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.iranjib_price_service import (  # noqa: E402
    PAGE_SOURCES,
    detect_anti_bot,
    fetch_price_page,
    parse_price_html,
)

OUTPUT_PATH = Path("data/iranjib_probe_sample.json")
RAW_HTML = {
    "domestic": Path("data/iranjib_domestic_probe_raw.html"),
    "imported": Path("data/iranjib_imported_probe_raw.html"),
}


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print("=" * 72)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def probe_page(session: requests.Session, *, kind: str, url: str, limit_rows: int) -> dict[str, Any]:
    html = fetch_price_page(session, url=url)
    raw_path = RAW_HTML[kind]
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(html, encoding="utf-8")

    anti_bot = detect_anti_bot(html, 200)
    rows = parse_price_html(html, page_kind=kind, page_url=url)
    statuses = Counter(row.status for row in rows)
    sample = [
        {
            "section": row.section_raw,
            "title": row.title_raw,
            "iid": row.iranjib_iid,
            "market_price_raw": row.market_price_raw,
            "factory_price_raw": row.factory_price_raw,
            "change_percent_raw": row.change_percent_raw,
            "change_amount_raw": row.change_amount_raw,
            "status": row.status,
            "market_price_toman": row.market_price_toman,
            "factory_price_toman": row.factory_price_toman,
        }
        for row in rows[: max(1, limit_rows)]
    ]
    return {
        "kind": kind,
        "url": url,
        "html_bytes": len(html.encode("utf-8")),
        "anti_bot": anti_bot,
        "raw_html_path": str(raw_path),
        "data_row_count": len(rows),
        "rows_with_iid": sum(1 for row in rows if row.iranjib_iid is not None),
        "rows_with_price": sum(1 for row in rows if row.market_price_toman is not None),
        "status_counts": dict(statuses),
        "sections": sorted({row.section_raw for row in rows if row.section_raw}),
        "sample_rows": sample,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("domestic", "imported", "all"), default="all")
    parser.add_argument("--limit-rows", type=int, default=12)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)

    sources = PAGE_SOURCES if args.kind == "all" else tuple(
        (kind, url) for kind, url in PAGE_SOURCES if kind == args.kind
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        }
    )

    results: list[dict[str, Any]] = []
    exit_code = 0
    for kind, url in sources:
        _print_section(f"{kind}  {url}")
        try:
            report = probe_page(session, kind=kind, url=url, limit_rows=args.limit_rows)
        except (Timeout, RequestException, Exception) as exc:
            print(f"FETCH/PARSE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1
            results.append({"kind": kind, "url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue

        results.append(report)
        print(f"html_bytes      : {report['html_bytes']}")
        print(f"anti_bot        : {report['anti_bot'] or '(none)'}")
        print(f"data_rows       : {report['data_row_count']}")
        print(f"rows_with_iid   : {report['rows_with_iid']}")
        print(f"rows_with_price : {report['rows_with_price']}")
        print(f"status_counts   : {report['status_counts']}")
        print(f"sections        : {report['sections']}")
        print(f"raw_html        : {report['raw_html_path']}")
        print("sample_rows:")
        print(json.dumps(report["sample_rows"], ensure_ascii=False, indent=2))
        if report["anti_bot"] or report["data_row_count"] <= 0:
            exit_code = 1
        if args.verbose:
            print(json.dumps(report, ensure_ascii=False, indent=2))

    payload = {"probed_at": _now_iso(), "pages": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
