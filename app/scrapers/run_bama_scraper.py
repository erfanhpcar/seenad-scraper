#!/usr/bin/env python3
"""
Orchestrate Bama.ir car spec scraping from a URL seed file.

Reads ``bama_specs_urls.txt``, fetches each page via ``BamaSpecsParser``, and appends
successful results to ``bama_scraped_specs.jsonl``. Progress is checkpointed in
``app/scrapers/scraper_progress.json`` so restarts skip finished URLs.

Usage::

    python app/scrapers/run_bama_scraper.py
    python app/scrapers/run_bama_scraper.py --limit 5
    python app/scrapers/run_bama_scraper.py --retry-failed
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scrapers.bama_specs_parser import BamaSpecsParser  # noqa: E402

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URLS_FILE = PROJECT_ROOT / "bama_specs_urls.txt"
DEFAULT_JSONL_FILE = PROJECT_ROOT / "bama_scraped_specs.jsonl"
DEFAULT_PROGRESS_FILE = Path(__file__).resolve().parent / "scraper_progress.json"

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
        return {"processed": {}}
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Corrupt progress file %s — starting fresh: %s", progress_path, exc)
        return {"processed": {}}
    if not isinstance(data.get("processed"), dict):
        data["processed"] = {}
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


def should_skip_url(
    url: str,
    processed: dict[str, Any],
    *,
    retry_failed: bool,
) -> bool:
    entry = processed.get(url)
    if not entry:
        return False
    status = entry.get("status")
    if status == "SUCCESS":
        return True
    if status == "FAILED" and not retry_failed:
        return True
    return False


def pending_urls(
    all_urls: list[str],
    processed: dict[str, Any],
    *,
    retry_failed: bool,
    limit: int | None,
) -> list[str]:
    queue = [u for u in all_urls if not should_skip_url(u, processed, retry_failed=retry_failed)]
    if limit is not None:
        queue = queue[:limit]
    return queue


def process_url(url: str) -> tuple[Status, dict[str, Any] | None, str | None]:
    parser = BamaSpecsParser(url)
    html = parser.fetch_html()
    specs = parser.parse_specs(html)
    if not specs:
        return "FAILED", None, "parse_specs returned empty sections"
    record = {
        "url": url,
        "scraped_at": _utc_now_iso(),
        "section_count": len(specs),
        "item_count": sum(len(section) for section in specs.values()),
        "specs": specs,
    }
    return "SUCCESS", record, None


def run_scraper(
    urls_path: Path,
    jsonl_path: Path,
    progress_path: Path,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    delay_min_s: float = DELAY_MIN_S,
    delay_max_s: float = DELAY_MAX_S,
) -> dict[str, int]:
    all_urls = load_url_queue(urls_path)
    progress = load_progress(progress_path)
    processed = progress["processed"]

    queue = pending_urls(
        all_urls,
        processed,
        retry_failed=retry_failed,
        limit=limit,
    )
    total_in_seed = len(all_urls)
    already_done = sum(
        1 for url in all_urls if should_skip_url(url, processed, retry_failed=retry_failed)
    )

    LOGGER.info(
        "Seed: %d URLs | already done: %d | this run: %d",
        total_in_seed,
        already_done,
        len(queue),
    )

    stats = {"success": 0, "failed": 0, "skipped": already_done}

    for index, url in enumerate(queue, start=1):
        position = already_done + index
        print(f"Processing [{position}/{total_in_seed}] {url}", flush=True)

        try:
            status, record, error_message = process_url(url)
        except Exception as exc:
            status = "FAILED"
            record = None
            error_message = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Failed to scrape %s", url)

        timestamp = _utc_now_iso()
        processed[url] = {
            "status": status,
            "scraped_at": timestamp,
            "error": error_message,
        }

        if status == "SUCCESS" and record is not None:
            append_jsonl_record(jsonl_path, record)
            stats["success"] += 1
            LOGGER.info(
                "OK [%d/%d] sections=%d items=%d",
                position,
                total_in_seed,
                record["section_count"],
                record["item_count"],
            )
        else:
            stats["failed"] += 1
            LOGGER.warning("FAIL [%d/%d] %s", position, total_in_seed, error_message)

        save_progress(progress_path, progress)

        if index < len(queue):
            delay = random.uniform(delay_min_s, delay_max_s)
            LOGGER.debug("Sleeping %.2fs before next request", delay)
            time.sleep(delay)

    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Bama car spec scraper pipeline.")
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
        help="Process only the first N pending URLs this run",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-process URLs previously marked FAILED",
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
            limit=args.limit,
            retry_failed=args.retry_failed,
            delay_min_s=args.delay_min,
            delay_max_s=args.delay_max,
        )
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 1

    print(
        f"Run complete — success: {stats['success']}, "
        f"failed: {stats['failed']}, skipped (prior): {stats['skipped']}"
    )
    print(f"Progress: {args.progress_file.resolve()}")
    print(f"Output:   {args.output.resolve()}")
    return 0 if stats["failed"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
