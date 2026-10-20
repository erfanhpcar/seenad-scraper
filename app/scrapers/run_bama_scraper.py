#!/usr/bin/env python3
"""
Orchestrate Bama.ir car spec scraping at trim level.

Each car-reviews page is one trim. Sibling trims are harvested from ``.chips``
links on the page (the sitemap can miss a tab). Successful pages are appended
to JSONL and compiled into a nested JSON file::

    { brand → model → trims[] → specs / review }

Usage::

    python app/scrapers/run_bama_scraper.py --refresh
    python app/scrapers/run_bama_scraper.py --limit 1
    python app/scrapers/run_bama_scraper.py --retry-failed
    docker exec -i seenad_backend python3 app/scrapers/run_bama_scraper.py --refresh
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scrapers.bama_specs_parser import BamaSpecsParser  # noqa: E402

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URLS_FILE = PROJECT_ROOT / "bama_specs_urls.txt"
DEFAULT_JSONL_FILE = PROJECT_ROOT / "data" / "raw_bama_car_specs.jsonl"
DEFAULT_JSON_FILE = PROJECT_ROOT / "data" / "raw_bama_car_specs.json"
DEFAULT_PROGRESS_FILE = PROJECT_ROOT / "data" / "bama_specs_progress.json"

Status = Literal["SUCCESS", "FAILED"]
DELAY_MIN_S = 1.5
DELAY_MAX_S = 3.5


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_url_queue(urls_path: Path) -> list[str]:
    if not urls_path.is_file():
        raise FileNotFoundError(f"URL seed file not found: {urls_path}")

    urls: list[str] = []
    seen: set[str] = set()
    for line in urls_path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def load_progress(progress_path: Path) -> dict[str, Any]:
    if not progress_path.is_file():
        return {"processed": {}, "discovered": []}
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Corrupt progress file %s — starting fresh: %s", progress_path, exc)
        return {"processed": {}, "discovered": []}
    if not isinstance(data.get("processed"), dict):
        data["processed"] = {}
    if not isinstance(data.get("discovered"), list):
        data["discovered"] = []
    return data


def save_progress(progress_path: Path, progress: dict[str, Any]) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress["updated_at"] = _utc_now_iso()
    tmp_path = progress_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(progress_path)


def append_jsonl_record(jsonl_path: Path, record: dict[str, Any]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl_records(jsonl_path: Path) -> list[dict[str, Any]]:
    if not jsonl_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("url"):
            records.append(payload)
    return records


def should_skip_url(
    url: str,
    processed: dict[str, Any],
    *,
    retry_failed: bool,
    refresh: bool,
) -> bool:
    entry = processed.get(url)
    if not entry:
        return False
    status = entry.get("status")
    if status == "SUCCESS":
        return not refresh
    if status == "FAILED" and not retry_failed and not refresh:
        return True
    return False


def compile_nested_json(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Group flat trim records into brand → model → trims."""
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        url = str(record.get("url") or "")
        if url:
            latest[url] = record

    models: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in latest.values():
        brand = str(record.get("brand_slug") or "")
        model = str(record.get("model_slug") or "")
        model_id = str(record.get("model_id") or "")
        key = (brand, model, model_id)
        bucket = models.setdefault(
            key,
            {
                "brand_slug": brand,
                "model_slug": model,
                "model_id": model_id,
                "title": record.get("title") or "",
                "trims": [],
            },
        )
        if record.get("title"):
            bucket["title"] = record["title"]
        bucket["trims"].append(
            {
                "trim_slug": record.get("trim_slug") or "",
                "trim_name": record.get("trim_name") or "",
                "url": record.get("url"),
                "year_label": record.get("year_label") or "",
                "review": record.get("review") or {},
                "section_count": record.get("section_count", 0),
                "item_count": record.get("item_count", 0),
                "specs": record.get("specs") or {},
                "scraped_at": record.get("scraped_at"),
            }
        )

    model_list: list[dict[str, Any]] = []
    for key in sorted(models):
        item = models[key]
        item["trims"].sort(key=lambda t: (t.get("trim_name") or "", t.get("trim_slug") or ""))
        item["trim_count"] = len(item["trims"])
        model_list.append(item)

    return {
        "source": "https://bama.ir/car-reviews",
        "crawled_at": _utc_now_iso(),
        "model_count": len(model_list),
        "trim_count": sum(m["trim_count"] for m in model_list),
        "models": model_list,
    }


def write_nested_json(json_path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = compile_nested_json(records)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = json_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(json_path)
    return payload


def process_url(url: str) -> tuple[Status, dict[str, Any] | None, str | None, list[str]]:
    parser = BamaSpecsParser(url)
    html = parser.fetch_html()
    page = parser.parse_page(html)
    sibling_urls = [item["url"] for item in page.get("trims") or [] if item.get("url")]
    if not page.get("specs"):
        return "FAILED", None, "parse_page returned empty specs", sibling_urls
    record = {
        "url": url,
        "scraped_at": _utc_now_iso(),
        "brand_slug": page.get("brand_slug"),
        "model_slug": page.get("model_slug"),
        "model_id": page.get("model_id"),
        "trim_slug": page.get("trim_slug"),
        "trim_name": page.get("trim_name"),
        "title": page.get("title"),
        "year_label": page.get("year_label"),
        "trims": page.get("trims") or [],
        "review": page.get("review") or {},
        "section_count": page.get("section_count", 0),
        "item_count": page.get("item_count", 0),
        "specs": page.get("specs") or {},
    }
    return "SUCCESS", record, None, sibling_urls


def run_scraper(
    urls_path: Path,
    jsonl_path: Path,
    progress_path: Path,
    json_path: Path | None = None,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    refresh: bool = False,
    delay_min_s: float = DELAY_MIN_S,
    delay_max_s: float = DELAY_MAX_S,
) -> dict[str, int]:
    seed_urls = load_url_queue(urls_path)
    progress = load_progress(progress_path)
    processed = progress["processed"]
    discovered = [u for u in progress.get("discovered") or [] if isinstance(u, str)]

    seen: set[str] = set()
    queue: deque[str] = deque()
    for url in seed_urls + discovered:
        if url not in seen:
            seen.add(url)
            if not should_skip_url(
                url, processed, retry_failed=retry_failed, refresh=refresh
            ):
                queue.append(url)

    already_done = sum(
        1
        for url in seen
        if should_skip_url(url, processed, retry_failed=retry_failed, refresh=refresh)
    )
    LOGGER.info(
        "Seed+discovered: %d URLs | already done: %d | queued: %d | refresh=%s",
        len(seen),
        already_done,
        len(queue),
        refresh,
    )

    stats = {"success": 0, "failed": 0, "skipped": already_done, "harvested": 0}
    processed_this_run = 0

    while queue:
        if limit is not None and processed_this_run >= limit:
            break

        url = queue.popleft()
        processed_this_run += 1
        position = already_done + processed_this_run
        print(f"Processing [{position}] {url}", flush=True)

        try:
            status, record, error_message, sibling_urls = process_url(url)
        except Exception as exc:
            status = "FAILED"
            record = None
            error_message = f"{type(exc).__name__}: {exc}"
            sibling_urls = []
            LOGGER.exception("Failed to scrape %s", url)

        timestamp = _utc_now_iso()
        processed[url] = {
            "status": status,
            "scraped_at": timestamp,
            "error": error_message,
        }

        for sibling in sibling_urls:
            if sibling in seen:
                continue
            seen.add(sibling)
            discovered.append(sibling)
            stats["harvested"] += 1
            if not should_skip_url(
                sibling, processed, retry_failed=retry_failed, refresh=refresh
            ):
                queue.append(sibling)
                LOGGER.info("Harvested sibling trim: %s", sibling)

        progress["discovered"] = discovered

        if status == "SUCCESS" and record is not None:
            append_jsonl_record(jsonl_path, record)
            stats["success"] += 1
            LOGGER.info(
                "OK [%d] %s / %s sections=%d items=%d trims=%d",
                position,
                record.get("title") or record.get("model_slug"),
                record.get("trim_name") or record.get("trim_slug") or "(default)",
                record["section_count"],
                record["item_count"],
                len(record.get("trims") or []),
            )
        else:
            stats["failed"] += 1
            LOGGER.warning("FAIL [%d] %s", position, error_message)

        save_progress(progress_path, progress)

        if queue and (limit is None or processed_this_run < limit):
            delay = random.uniform(delay_min_s, delay_max_s)
            LOGGER.debug("Sleeping %.2fs before next request", delay)
            time.sleep(delay)

    if json_path is not None:
        payload = write_nested_json(json_path, load_jsonl_records(jsonl_path))
        LOGGER.info(
            "Wrote nested JSON %s (models=%d trims=%d)",
            json_path,
            payload["model_count"],
            payload["trim_count"],
        )

    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Bama car spec scraper at trim level.")
    parser.add_argument(
        "--urls-file",
        type=Path,
        default=DEFAULT_URLS_FILE,
        help=f"Seed URL list (default: {DEFAULT_URLS_FILE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_JSONL_FILE,
        help=f"JSON Lines output file (default: {DEFAULT_JSONL_FILE})",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=DEFAULT_JSON_FILE,
        help=f"Nested JSON output (default: {DEFAULT_JSON_FILE})",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip writing the nested JSON file",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=DEFAULT_PROGRESS_FILE,
        help=f"Checkpoint JSON (default: {DEFAULT_PROGRESS_FILE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only N URLs this run (harvested siblings still enqueue)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-process URLs previously marked FAILED",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch URLs previously marked SUCCESS (full recrawl)",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=DELAY_MIN_S,
        help=f"Minimum random delay between requests (default: {DELAY_MIN_S})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=DELAY_MAX_S,
        help=f"Maximum random delay between requests (default: {DELAY_MAX_S})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s | %(message)s",
    )

    if args.delay_min < 0 or args.delay_max < args.delay_min:
        LOGGER.error("Invalid delay range: min=%s max=%s", args.delay_min, args.delay_max)
        return 1

    try:
        stats = run_scraper(
            args.urls_file,
            args.output,
            args.progress_file,
            None if args.no_json else args.json_output,
            limit=args.limit,
            retry_failed=args.retry_failed,
            refresh=args.refresh,
            delay_min_s=args.delay_min,
            delay_max_s=args.delay_max,
        )
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 1

    print(
        f"Run complete — success: {stats['success']}, "
        f"failed: {stats['failed']}, skipped (prior): {stats['skipped']}, "
        f"harvested: {stats['harvested']}"
    )
    print(f"Progress: {args.progress_file.resolve()}")
    print(f"JSONL:    {args.output.resolve()}")
    if not args.no_json:
        print(f"JSON:     {args.json_output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
