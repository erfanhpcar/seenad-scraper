#!/usr/bin/env python3
"""
Scraper Server health dashboard: zero-price snapshots, raw listing volume, and network errors.

Queries PostgreSQL (car_zero_price_scrape_snapshot, scraped_listings, optional pipeline/log
tables) and optionally scans Celery worker container logs for HTTP/timeout failures in the
last 24 hours.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

_PROVIDER_SOURCE_SQL = """
    CASE
        WHEN sl.source = 'divar' OR sl.source LIKE 'divar_external:%' THEN 'divar'
        WHEN sl.source = 'bama' THEN 'bama'
        WHEN sl.source IN ('hm', 'hamrah-mechanic') THEN 'hamrah_mechanic'
        WHEN sl.source IN ('kn', 'karnameh') THEN 'karnameh'
        ELSE sl.source
    END
"""

_PIPELINE_HISTORY_CANDIDATES: tuple[str, ...] = (
    "mapping_pipeline_history",
    "scrape_pipeline_history",
    "scraping_pipeline_history",
    "worker_scrape_log",
    "scrape_log",
)

_LOG_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"reason=(\w+)", re.IGNORECASE), "reason_tag"),
    (re.compile(r"kind=(ReadTimeout|ConnectTimeout|Timeout|HTTPError)", re.IGNORECASE), "retry_kind"),
    (re.compile(r"(\d{3})\s+Server Error", re.IGNORECASE), "http_status"),
    (re.compile(r"Read timed out", re.IGNORECASE), "read_timeout"),
    (re.compile(r"handshake operation timed out", re.IGNORECASE), "ssl_handshake_timeout"),
    (re.compile(r"fetch failed", re.IGNORECASE), "fetch_failed"),
    (re.compile(r"No URLs found", re.IGNORECASE), "empty_discovery"),
    (re.compile(r"status=redis_error", re.IGNORECASE), "redis_error"),
    (re.compile(r"\] FAILURE/", re.IGNORECASE), "celery_failure"),
    (re.compile(r"Task .* raised unexpected", re.IGNORECASE), "celery_exception"),
)


@dataclass(frozen=True)
class ZeroPriceBatchStat:
    batch_id: str
    created_at_utc: datetime
    raw_rows: int
    distinct_brands: int


@dataclass(frozen=True)
class ListingDayProviderStat:
    day: date
    provider: str
    row_count: int


@dataclass(frozen=True)
class ParseErrorStat:
    provider: str
    parse_error_count: int


@dataclass(frozen=True)
class LogErrorSummary:
    source: str
    total_lines: int
    by_category: dict[str, int]
    sample_lines: list[str]


@dataclass(frozen=True)
class CeleryFailureStat:
    category: str
    failure_count: int
    sample_detail: str | None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Scraper Server health report (DB + optional worker logs).",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default=os.getenv("REPORT_TIMEZONE", "Asia/Tehran"),
        help="IANA timezone for display (default: Asia/Tehran).",
    )
    parser.add_argument(
        "--listing-days",
        type=int,
        default=3,
        help="Number of calendar days of scraped_listings volume to include (default: 3).",
    )
    parser.add_argument(
        "--zero-price-batches",
        type=int,
        default=5,
        help="Number of latest zero-price snapshot batches to show (default: 5).",
    )
    parser.add_argument(
        "--error-hours",
        type=int,
        default=24,
        help="Lookback window in hours for error/network health (default: 24).",
    )
    parser.add_argument(
        "--scan-worker-logs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scan docker compose worker logs for network/HTTP issues (default: auto).",
    )
    parser.add_argument(
        "--compose-project-dir",
        type=str,
        default="",
        help="Directory containing docker-compose.yml for log scan (default: repo root).",
    )
    parser.add_argument(
        "--worker-log-file",
        type=str,
        default=os.getenv("WORKER_LOG_FILE", "").strip(),
        help="Optional worker log file to scan when docker compose is unavailable.",
    )
    return parser


def _format_int(value: int) -> str:
    return f"{value:,}"


def _to_local_display(dt_utc: datetime, tz: ZoneInfo) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    local = dt_utc.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    return widths


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["  (no rows)"]
    widths = _column_widths(headers, rows)
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    header_line = "|" + "|".join(f" {headers[i]:<{widths[i]}} " for i in range(len(headers))) + "|"
    data_lines = [
        "|" + "|".join(f" {row[i]:<{widths[i]}} " for i in range(len(headers))) + "|"
        for row in rows
    ]
    return [border, header_line, border, *data_lines, border]


def _discover_pipeline_history_table(session: Any) -> str | None:
    for candidate in _PIPELINE_HISTORY_CANDIDATES:
        row = session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = :table_name
                LIMIT 1
                """
            ),
            {"table_name": candidate},
        ).first()
        if row:
            return candidate
    return None


def _pick_timestamp_column(session: Any, table_name: str) -> str | None:
    for candidate in ("created_at", "run_finished_at", "logged_at", "run_at", "updated_at"):
        row = session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"table_name": table_name, "column_name": candidate},
        ).first()
        if row:
            return candidate
    return None


def _fetch_zero_price_batch_stats(session: Any, *, limit: int) -> list[ZeroPriceBatchStat]:
    query = text(
        """
        WITH ranked_batches AS (
            SELECT
                batch_id,
                MIN(created_at) AS batch_created_at,
                COUNT(*)::bigint AS raw_rows,
                COUNT(
                    DISTINCT NULLIF(TRIM(brand_name_raw), '')
                )::bigint AS distinct_brands,
                ROW_NUMBER() OVER (ORDER BY MIN(created_at) DESC) AS rn
            FROM car_zero_price_scrape_snapshot
            GROUP BY batch_id
        )
        SELECT batch_id, batch_created_at, raw_rows, distinct_brands
        FROM ranked_batches
        WHERE rn <= :limit
        ORDER BY batch_created_at DESC
        """
    )
    rows = session.execute(query, {"limit": limit}).mappings().all()
    return [
        ZeroPriceBatchStat(
            batch_id=str(row["batch_id"]),
            created_at_utc=row["batch_created_at"],
            raw_rows=int(row["raw_rows"]),
            distinct_brands=int(row["distinct_brands"]),
        )
        for row in rows
    ]


def _listings_timestamp_column(session: Any) -> str:
    for candidate in ("created_at", "scraped_at"):
        row = session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'scraped_listings'
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"column_name": candidate},
        ).first()
        if row:
            return candidate
    return "scraped_at"


def _fetch_listing_stats(
    session: Any,
    *,
    listing_days: int,
    tz: ZoneInfo,
) -> tuple[str, list[ListingDayProviderStat]]:
    ts_col = _listings_timestamp_column(session)
    start_local = datetime.now(tz).date() - timedelta(days=listing_days - 1)
    start_utc = datetime.combine(start_local, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    query = text(
        f"""
        SELECT
            (sl.{ts_col} AT TIME ZONE :tz_name)::date AS day_local,
            {_PROVIDER_SOURCE_SQL} AS provider,
            COUNT(*)::bigint AS row_count
        FROM scraped_listings sl
        WHERE sl.{ts_col} >= :start_utc
        GROUP BY day_local, provider
        ORDER BY day_local DESC, row_count DESC, provider
        """
    )
    rows = session.execute(
        query,
        {"start_utc": start_utc, "tz_name": str(tz)},
    ).mappings().all()

    stats = [
        ListingDayProviderStat(
            day=row["day_local"],
            provider=str(row["provider"] or "unknown"),
            row_count=int(row["row_count"]),
        )
        for row in rows
    ]
    return ts_col, stats


def _fetch_parse_error_stats(session: Any, *, since_utc: datetime) -> list[ParseErrorStat]:
    query = text(
        f"""
        SELECT
            {_PROVIDER_SOURCE_SQL} AS provider,
            COUNT(*)::bigint AS parse_error_count
        FROM scraped_listings sl
        WHERE sl.scraped_at >= :since_utc
          AND COALESCE((sl.raw_data->>'parse_error')::boolean, false) = true
        GROUP BY provider
        ORDER BY parse_error_count DESC, provider
        """
    )
    rows = session.execute(query, {"since_utc": since_utc}).mappings().all()
    return [
        ParseErrorStat(provider=str(row["provider"] or "unknown"), parse_error_count=int(row["parse_error_count"]))
        for row in rows
    ]


def _fetch_pipeline_history_errors(
    session: Any,
    table_name: str,
    *,
    since_utc: datetime,
) -> list[dict[str, Any]]:
    ts_col = _pick_timestamp_column(session, table_name)
    if not ts_col:
        return []

    has_metadata = session.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
              AND column_name = 'metadata'
            LIMIT 1
            """
        ),
        {"table_name": table_name},
    ).first()

    if has_metadata:
        query = text(
            f"""
            SELECT
                id,
                {ts_col} AS event_at,
                metadata
            FROM {table_name}
            WHERE {ts_col} >= :since_utc
              AND (
                    COALESCE((metadata->>'errors_http')::int, 0) > 0
                 OR COALESCE((metadata->>'http_errors')::int, 0) > 0
                 OR COALESCE((metadata->>'error_count')::int, 0) > 0
                 OR metadata ? 'error'
                 OR metadata ? 'last_error'
              )
            ORDER BY {ts_col} DESC
            LIMIT 20
            """
        )
    else:
        query = text(
            f"""
            SELECT id, {ts_col} AS event_at
            FROM {table_name}
            WHERE {ts_col} >= :since_utc
            ORDER BY {ts_col} DESC
            LIMIT 0
            """
        )

    return [dict(row) for row in session.execute(query, {"since_utc": since_utc}).mappings().all()]


def _classify_log_line(line: str) -> str | None:
    lowered = line.lower()
    if "warning/" not in lowered and "error/" not in lowered and "failure/" not in lowered:
        if "celery_failure" not in lowered and "raised unexpected" not in lowered:
            return None

    for pattern, category in _LOG_ERROR_PATTERNS:
        if category == "reason_tag":
            match = pattern.search(line)
            if match:
                reason = match.group(1).lower()
                if reason in {
                    "http_error",
                    "read_timeout",
                    "connect_timeout",
                    "timeout",
                    "connection_error",
                    "dns_failure",
                    "ssl_handshake_failure",
                    "ssl_error",
                    "connection_reset_by_peer",
                }:
                    return reason
            continue
        if pattern.search(line):
            return category
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "server error" in lowered or "httperror" in lowered:
        return "http_error"
    return None


def _scan_log_text(source: str, text_blob: str) -> LogErrorSummary:
    categories: Counter[str] = Counter()
    samples: list[str] = []
    matched_lines = 0

    for raw_line in text_blob.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        category = _classify_log_line(line)
        if not category:
            continue
        matched_lines += 1
        categories[category] += 1
        if len(samples) < 8:
            compact = re.sub(r"\s+", " ", line)
            if len(compact) > 140:
                compact = compact[:137] + "..."
            samples.append(compact)

    return LogErrorSummary(
        source=source,
        total_lines=matched_lines,
        by_category=dict(categories.most_common()),
        sample_lines=samples,
    )


def _scan_worker_log_file(path: Path | str) -> LogErrorSummary | None:
    if path == "-":
        content = sys.stdin.read()
        if not content.strip():
            return None
        return _scan_log_text("stdin", content)

    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _scan_log_text(f"file:{file_path}", content)


def _scan_worker_logs(
    *,
    compose_dir: Path,
    error_hours: int,
) -> LogErrorSummary | None:
    since = f"{error_hours}h"
    try:
        completed = subprocess.run(
            ["docker", "compose", "logs", "worker", "--since", since],
            cwd=str(compose_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0 and not completed.stdout and not completed.stderr:
        return None

    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if not combined.strip():
        return LogErrorSummary(
            source=f"docker compose logs worker --since {since}",
            total_lines=0,
            by_category={},
            sample_lines=[],
        )

    return _scan_log_text(f"docker compose logs worker --since {since}", combined)


def _parse_celery_date_done(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _celery_failure_category(data: dict[str, Any]) -> str:
    result = data.get("result")
    if isinstance(result, dict):
        exc_type = str(result.get("exc_type") or "").strip()
        if exc_type:
            if exc_type == "HTTPError":
                messages = result.get("exc_message")
                if isinstance(messages, list) and messages:
                    msg = str(messages[0])
                    status_match = re.search(r"(\d{3})\s+Client Error|(\d{3})\s+Server Error", msg)
                    if status_match:
                        code = status_match.group(1) or status_match.group(2)
                        return f"http_{code}"
                return "http_error"
            if "Timeout" in exc_type:
                return exc_type
            return exc_type

    task_name = str(data.get("task_name") or data.get("name") or "").strip()
    if task_name:
        return task_name
    return "unknown_failure"


def _celery_failure_detail(data: dict[str, Any]) -> str | None:
    result = data.get("result")
    if isinstance(result, dict):
        messages = result.get("exc_message")
        if isinstance(messages, list) and messages:
            compact = re.sub(r"\s+", " ", str(messages[0]).strip())
            return compact[:200] + ("..." if len(compact) > 200 else "")
    traceback = data.get("traceback")
    if isinstance(traceback, str) and traceback.strip():
        compact = re.sub(r"\s+", " ", traceback.strip())
        return compact[:200] + ("..." if len(compact) > 200 else "")
    return None


def _fetch_celery_failures(*, since_utc: datetime, max_keys: int = 5000) -> list[CeleryFailureStat]:
    broker = os.getenv("CELERY_RESULT_BACKEND", os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"))
    try:
        client = redis.Redis.from_url(broker, decode_responses=True)
    except redis.RedisError:
        return []

    failures_by_category: Counter[str] = Counter()
    sample_details: dict[str, str] = {}
    scanned = 0

    try:
        for key in client.scan_iter(match="celery-task-meta-*", count=500):
            scanned += 1
            if scanned > max_keys:
                break
            try:
                payload = client.get(key)
                if not payload:
                    continue
                data = json.loads(payload)
            except (json.JSONDecodeError, redis.RedisError):
                continue

            if str(data.get("status", "")).upper() != "FAILURE":
                continue

            done_at = _parse_celery_date_done(data.get("date_done"))
            if done_at is None or done_at < since_utc:
                continue

            category = _celery_failure_category(data)
            failures_by_category[category] += 1
            if category not in sample_details:
                detail = _celery_failure_detail(data)
                if detail:
                    sample_details[category] = detail
    finally:
        try:
            client.close()
        except Exception:
            pass

    return [
        CeleryFailureStat(
            category=category,
            failure_count=count,
            sample_detail=sample_details.get(category),
        )
        for category, count in failures_by_category.most_common()
    ]


def _should_scan_logs(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.getenv("SCAN_WORKER_LOGS", "1").strip().lower() not in {"0", "false", "no"}


def _render_report(
    *,
    tz: ZoneInfo,
    zero_price_batches: list[ZeroPriceBatchStat],
    listings_ts_col: str,
    listing_stats: list[ListingDayProviderStat],
    parse_errors: list[ParseErrorStat],
    pipeline_table: str | None,
    pipeline_errors: list[dict[str, Any]],
    log_summary: LogErrorSummary | None,
    celery_failures: list[CeleryFailureStat],
    error_hours: int,
    listing_days: int,
) -> str:
    now_utc = datetime.now(timezone.utc)
    lines: list[str] = [
        "",
        "╔══════════════════════════════════════════════════════════════════════════════╗",
        "║                     SCRAPER SERVER HEALTH REPORT                             ║",
        "╚══════════════════════════════════════════════════════════════════════════════╝",
        "",
        f"Generated (UTC) : {now_utc.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Display TZ      : {tz}",
        f"Error window    : last {error_hours} hours",
        "",
        "┌──────────────────────────────────────────────────────────────────────────────┐",
        "│ 1. Zero-Price Snapshot Stats (آخرین وضعیت قیمت صفر)                          │",
        "└──────────────────────────────────────────────────────────────────────────────┘",
        "",
        "_Latest batches in `car_zero_price_scrape_snapshot` grouped by `batch_id`._",
        "",
    ]

    zp_rows = [
        [
            item.batch_id,
            _to_local_display(item.created_at_utc, tz),
            _format_int(item.raw_rows),
            _format_int(item.distinct_brands),
        ]
        for item in zero_price_batches
    ]
    lines.extend(_render_table(["batch_id", "created_at", "raw_rows", "distinct_brands"], zp_rows))

    lines.extend(
        [
            "",
            "┌──────────────────────────────────────────────────────────────────────────────┐",
            "│ 2. Scraped Listings Stats (وضعیت آگهی‌های خام)                               │",
            "└──────────────────────────────────────────────────────────────────────────────┘",
            "",
            f"_Volume from `scraped_listings` by `{listings_ts_col}::date` (last {listing_days} days, "
            f"{tz} calendar days)._",
            "",
        ]
    )

    if listing_stats:
        days = sorted({item.day for item in listing_stats}, reverse=True)
        for day in days:
            day_rows = [
                [item.provider, _format_int(item.row_count)]
                for item in listing_stats
                if item.day == day
            ]
            day_total = sum(item.row_count for item in listing_stats if item.day == day)
            lines.append(f"  {day.isoformat()}  (total: {_format_int(day_total)})")
            lines.extend(_render_table(["provider", "rows"], day_rows))
            lines.append("")
    else:
        lines.append("  (no rows in the selected window)")
        lines.append("")

    lines.extend(
        [
            "┌──────────────────────────────────────────────────────────────────────────────┐",
            "│ 3. Error & Network Health (وضعیت خطاها)                                      │",
            "└──────────────────────────────────────────────────────────────────────────────┘",
            "",
        ]
    )

    total_parse_errors = sum(item.parse_error_count for item in parse_errors)
    if parse_errors:
        pe_rows = [[item.provider, _format_int(item.parse_error_count)] for item in parse_errors]
        lines.append(f"DB parse errors (`raw_data.parse_error=true`, last {error_hours}h): {_format_int(total_parse_errors)}")
        lines.extend(_render_table(["provider", "parse_errors"], pe_rows))
        lines.append("")
    else:
        lines.append(f"DB parse errors (`raw_data.parse_error=true`, last {error_hours}h): 0")
        lines.append("")

    if pipeline_table:
        if pipeline_errors:
            lines.append(f"Pipeline history (`{pipeline_table}`) flagged events: {len(pipeline_errors)}")
            for event in pipeline_errors[:5]:
                event_at = event.get("event_at")
                ts_display = (
                    _to_local_display(event_at, tz)
                    if isinstance(event_at, datetime)
                    else str(event_at)
                )
                lines.append(f"  - id={event.get('id')} at {ts_display} metadata={event.get('metadata')}")
            lines.append("")
        else:
            lines.append(f"Pipeline history (`{pipeline_table}`): no error metadata in the last {error_hours}h.")
            lines.append("")
    else:
        lines.append("Pipeline history: no pipeline/log table found in PostgreSQL.")
        lines.append("")

    if log_summary is None:
        lines.append(
            "Worker logs: skipped (docker compose / log file unavailable). "
            "Pass `--worker-log-file` or run with docker compose on the host."
        )
    elif log_summary.total_lines == 0:
        lines.append(f"Worker logs ({log_summary.source}): no HTTP/timeout/empty-response signals detected.")
    else:
        lines.append(f"Worker logs ({log_summary.source}): {_format_int(log_summary.total_lines)} matched line(s)")
        log_rows = [[category, _format_int(count)] for category, count in log_summary.by_category.items()]
        lines.extend(_render_table(["category", "count"], log_rows))
        if log_summary.sample_lines:
            lines.append("")
            lines.append("Recent samples:")
            for sample in log_summary.sample_lines:
                lines.append(f"  • {sample}")
    lines.append("")

    if celery_failures:
        total_failures = sum(item.failure_count for item in celery_failures)
        lines.append(f"Celery task failures (Redis result backend, last {error_hours}h): {_format_int(total_failures)}")
        cf_rows = [[item.category, _format_int(item.failure_count)] for item in celery_failures]
        lines.extend(_render_table(["category", "failures"], cf_rows))
        for item in celery_failures[:3]:
            if item.sample_detail:
                lines.append(f"  sample ({item.category}): {item.sample_detail}")
        lines.append("")
    else:
        lines.append(f"Celery task failures (Redis result backend, last {error_hours}h): 0")
        lines.append("")

    overall_ok = (
        total_parse_errors == 0
        and not pipeline_errors
        and not celery_failures
        and (log_summary is None or log_summary.total_lines == 0)
    )
    lines.extend(
        [
            "",
            "─" * 78,
            f"Overall network health (last {error_hours}h): "
            + ("OK — no significant errors detected" if overall_ok else "REVIEW — see sections above"),
            "",
        ]
    )
    return "\n".join(lines)


def generate_report(
    *,
    timezone_name: str = "Asia/Tehran",
    listing_days: int = 3,
    zero_price_batches: int = 5,
    error_hours: int = 24,
    scan_worker_logs: bool | None = None,
    compose_project_dir: str = "",
    worker_log_file: str = "",
) -> str:
    tz = ZoneInfo(timezone_name)
    since_utc = datetime.now(timezone.utc) - timedelta(hours=error_hours)
    compose_dir = Path(compose_project_dir) if compose_project_dir else Path(__file__).resolve().parents[1]

    with SessionLocal() as session:
        zp_stats = _fetch_zero_price_batch_stats(session, limit=zero_price_batches)
        listings_ts_col, listing_stats = _fetch_listing_stats(session, listing_days=listing_days, tz=tz)
        parse_errors = _fetch_parse_error_stats(session, since_utc=since_utc)
        pipeline_table = _discover_pipeline_history_table(session)
        pipeline_errors: list[dict[str, Any]] = []
        if pipeline_table:
            pipeline_errors = _fetch_pipeline_history_errors(session, pipeline_table, since_utc=since_utc)

    log_summary: LogErrorSummary | None = None
    if worker_log_file:
        log_summary = _scan_worker_log_file(worker_log_file)
    elif _should_scan_logs(scan_worker_logs):
        log_summary = _scan_worker_logs(compose_dir=compose_dir, error_hours=error_hours)

    celery_failures = _fetch_celery_failures(since_utc=since_utc)

    return _render_report(
        tz=tz,
        zero_price_batches=zp_stats,
        listings_ts_col=listings_ts_col,
        listing_stats=listing_stats,
        parse_errors=parse_errors,
        pipeline_table=pipeline_table,
        pipeline_errors=pipeline_errors,
        log_summary=log_summary,
        celery_failures=celery_failures,
        error_hours=error_hours,
        listing_days=listing_days,
    )


def main() -> int:
    args = _build_parser().parse_args()
    try:
        report = generate_report(
            timezone_name=args.timezone,
            listing_days=args.listing_days,
            zero_price_batches=args.zero_price_batches,
            error_hours=args.error_hours,
            scan_worker_logs=args.scan_worker_logs,
            compose_project_dir=args.compose_project_dir,
            worker_log_file=args.worker_log_file,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
