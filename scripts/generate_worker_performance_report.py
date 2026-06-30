#!/usr/bin/env python3
"""
End-of-day worker performance report for Seenad scraper/Celery workers.

Reads PostgreSQL (scraped_listings, optional sync log tables, mapping_pipeline_history)
and prints a formatted terminal/Markdown summary for the report date (default: today).

Sync log tables are auto-discovered when they expose rows_refined and rows_stamped.
Override with SYNC_LOG_TABLE or --sync-log-table. When absent, proxy metrics are used
and labeled in the report.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal

PRIMARY_PROVIDERS: tuple[str, ...] = ("divar", "bama", "hm", "kn")

_PROVIDER_SOURCE_SQL = """
    CASE
        WHEN sl.source = 'divar' OR sl.source LIKE 'divar_external:%' THEN 'divar'
        WHEN sl.source = 'bama' THEN 'bama'
        WHEN sl.source IN ('hm', 'hamrah-mechanic') THEN 'hm'
        WHEN sl.source IN ('kn', 'karnameh') THEN 'kn'
        ELSE NULL
    END
"""

_SYNC_LOG_CANDIDATES: tuple[str, ...] = (
    "worker_sync_log",
    "sync_log",
    "listing_sync_log",
    "worker_sync_stamp",
    "sync_stamp",
)


@dataclass(frozen=True)
class ReportWindow:
    report_date: date
    timezone_label: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class ProviderVolumetrics:
    provider: str
    rows_today: int


@dataclass(frozen=True)
class SyncEfficiency:
    source_label: str
    batches_processed: int
    rows_refined: int
    rows_stamped: int
    is_proxy: bool


@dataclass(frozen=True)
class HealthFlag:
    provider: str
    rows_today: int
    severity: str
    message: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate end-of-day Seenad worker performance report from PostgreSQL.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="Report date (YYYY-MM-DD). Default: today in --timezone.",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default=os.getenv("REPORT_TIMEZONE", "Asia/Tehran"),
        help="IANA timezone for the report day window (default: Asia/Tehran or REPORT_TIMEZONE).",
    )
    parser.add_argument(
        "--sync-log-table",
        type=str,
        default=os.getenv("SYNC_LOG_TABLE", "").strip(),
        help="Explicit sync log table name (default: auto-discover or SYNC_LOG_TABLE).",
    )
    return parser


def _parse_report_date(raw: str, tz: ZoneInfo) -> date:
    if not raw:
        return datetime.now(tz).date()
    return date.fromisoformat(raw)


def _report_window(report_date: date, tz_name: str) -> ReportWindow:
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(report_date, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return ReportWindow(
        report_date=report_date,
        timezone_label=tz_name,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
    )


def _discover_sync_log_table(session: Any, explicit: str) -> str | None:
    if explicit:
        exists = session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = :table_name
                LIMIT 1
                """
            ),
            {"table_name": explicit},
        ).first()
        return explicit if exists else None

    for candidate in _SYNC_LOG_CANDIDATES:
        row = session.execute(
            text(
                """
                SELECT c.table_name
                FROM information_schema.columns c
                WHERE c.table_schema = 'public'
                  AND c.table_name = :table_name
                  AND c.column_name IN ('rows_refined', 'rows_stamped')
                GROUP BY c.table_name
                HAVING COUNT(DISTINCT c.column_name) = 2
                """
            ),
            {"table_name": candidate},
        ).first()
        if row:
            return str(row[0])

    row = session.execute(
        text(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_name IN ('rows_refined', 'rows_stamped')
            GROUP BY c.table_name
            HAVING COUNT(DISTINCT c.column_name) = 2
            ORDER BY c.table_name
            LIMIT 1
            """
        )
    ).first()
    return str(row[0]) if row else None


def _pick_timestamp_column(session: Any, table_name: str) -> str:
    for candidate in ("created_at", "logged_at", "run_at", "stamped_at", "updated_at"):
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
    raise RuntimeError(f"sync log table `{table_name}` has no recognized timestamp column")


def _table_has_column(session: Any, table_name: str, column_name: str) -> bool:
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
        {"table_name": table_name, "column_name": column_name},
    ).first()
    return row is not None


def _fetch_provider_volumetrics(session: Any, window: ReportWindow) -> list[ProviderVolumetrics]:
    query = text(
        f"""
        SELECT provider, COUNT(*)::bigint AS rows_today
        FROM (
            SELECT {_PROVIDER_SOURCE_SQL} AS provider
            FROM scraped_listings sl
            WHERE sl.scraped_at >= :start_utc
              AND sl.scraped_at < :end_utc
        ) touched
        WHERE provider IS NOT NULL
        GROUP BY provider
        ORDER BY provider
        """
    )
    rows = session.execute(
        query,
        {"start_utc": window.start_utc, "end_utc": window.end_utc},
    ).mappings().all()
    by_provider = {str(row["provider"]): int(row["rows_today"]) for row in rows}
    return [
        ProviderVolumetrics(provider=provider, rows_today=by_provider.get(provider, 0))
        for provider in PRIMARY_PROVIDERS
    ]


def _fetch_sync_from_log_table(
    session: Any,
    table_name: str,
    window: ReportWindow,
) -> SyncEfficiency:
    ts_col = _pick_timestamp_column(session, table_name)
    has_batch_id = _table_has_column(session, table_name, "batch_id")
    batch_expr = "COUNT(DISTINCT batch_id)" if has_batch_id else "COUNT(*)"
    query = text(
        f"""
        SELECT
            {batch_expr}::bigint AS batches_processed,
            COALESCE(SUM(rows_refined), 0)::bigint AS rows_refined,
            COALESCE(SUM(rows_stamped), 0)::bigint AS rows_stamped
        FROM {table_name}
        WHERE {ts_col} >= :start_utc
          AND {ts_col} < :end_utc
        """
    )
    row = session.execute(
        query,
        {"start_utc": window.start_utc, "end_utc": window.end_utc},
    ).mappings().one()
    return SyncEfficiency(
        source_label=f"public.{table_name} ({ts_col})",
        batches_processed=int(row["batches_processed"]),
        rows_refined=int(row["rows_refined"]),
        rows_stamped=int(row["rows_stamped"]),
        is_proxy=False,
    )


def _fetch_sync_proxy(session: Any, window: ReportWindow) -> SyncEfficiency:
    pipeline_row = session.execute(
        text(
            """
            SELECT COUNT(*)::bigint AS batches_processed
            FROM mapping_pipeline_history
            WHERE created_at >= :start_utc
              AND created_at < :end_utc
            """
        ),
        {"start_utc": window.start_utc, "end_utc": window.end_utc},
    ).mappings().one()

    refined_row = session.execute(
        text(
            """
            SELECT COUNT(*)::bigint AS rows_refined
            FROM listings_clean
            WHERE updated_at >= :start_utc
              AND updated_at < :end_utc
            """
        ),
        {"start_utc": window.start_utc, "end_utc": window.end_utc},
    ).mappings().one()

    stamped_row = session.execute(
        text(
            f"""
            SELECT COUNT(*)::bigint AS rows_stamped
            FROM scraped_listings sl
            WHERE sl.scraped_at >= :start_utc
              AND sl.scraped_at < :end_utc
              AND sl.car_reference_id IS NOT NULL
              AND ({_PROVIDER_SOURCE_SQL}) IS NOT NULL
            """
        ),
        {"start_utc": window.start_utc, "end_utc": window.end_utc},
    ).mappings().one()

    return SyncEfficiency(
        source_label=(
            "proxy: mapping_pipeline_history (batches), "
            "listings_clean.updated_at (refined), "
            "scraped_listings.car_reference_id (stamped)"
        ),
        batches_processed=int(pipeline_row["batches_processed"]),
        rows_refined=int(refined_row["rows_refined"]),
        rows_stamped=int(stamped_row["rows_stamped"]),
        is_proxy=True,
    )


def _build_health_flags(volumetrics: list[ProviderVolumetrics]) -> list[HealthFlag]:
    flags: list[HealthFlag] = []
    for item in volumetrics:
        if item.rows_today == 0:
            flags.append(
                HealthFlag(
                    provider=item.provider,
                    rows_today=0,
                    severity="CRITICAL",
                    message="ATTENTION REQUIRED — zero rows ingested/upserted today",
                )
            )
        elif item.rows_today < 10:
            flags.append(
                HealthFlag(
                    provider=item.provider,
                    rows_today=item.rows_today,
                    severity="WARN",
                    message="Low volume — review worker schedule and upstream feeds",
                )
            )
    if not flags:
        flags.append(
            HealthFlag(
                provider="all",
                rows_today=sum(v.rows_today for v in volumetrics),
                severity="OK",
                message="All primary providers reported activity today",
            )
        )
    return flags


def _format_int(value: int) -> str:
    return f"{value:,}"


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    return widths


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_(no rows)_"]
    widths = _column_widths(headers, rows)
    sep = "|".join("-" * (width + 2) for width in widths)
    header_line = "|".join(f" {headers[i]:<{widths[i]}} " for i in range(len(headers)))
    data_lines = [
        "|".join(f" {row[i]:<{widths[i]}} " for i in range(len(headers)))
        for row in rows
    ]
    return [header_line, sep, *data_lines]


def _render_report(
    window: ReportWindow,
    volumetrics: list[ProviderVolumetrics],
    sync: SyncEfficiency,
    health_flags: list[HealthFlag],
) -> str:
    total_ingested = sum(item.rows_today for item in volumetrics)
    lines: list[str] = [
        "",
        "╔══════════════════════════════════════════════════════════════════════════════╗",
        "║                    SEENAD WORKER PERFORMANCE REPORT                          ║",
        "╚══════════════════════════════════════════════════════════════════════════════╝",
        "",
        f"Report date     : {window.report_date.isoformat()} ({window.timezone_label})",
        f"Window (UTC)    : {window.start_utc.isoformat()} → {window.end_utc.isoformat()}",
        f"Generated at    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        "## 1. Scraper Volumetrics (Today's Ingestion)",
        "",
        "_Counts rows in `scraped_listings` with `scraped_at` in the report window "
        "(inserts and upserts both refresh `scraped_at`)._",
        "",
    ]
    vol_rows = [
        [item.provider, _format_int(item.rows_today)]
        for item in volumetrics
    ]
    vol_rows.append(["**TOTAL**", _format_int(total_ingested)])
    lines.extend(_render_table(["Provider", "Rows Today"], vol_rows))
    lines.extend(
        [
            "",
            "## 2. Worker Efficiency & Sync Logs",
            "",
        ]
    )
    if sync.is_proxy:
        lines.append(
            "_No dedicated sync log table found (`rows_refined` / `rows_stamped`). "
            "Showing proxy metrics — configure `SYNC_LOG_TABLE` when available._"
        )
        lines.append("")
    else:
        lines.append(f"_Source: `{sync.source_label}`_")
        lines.append("")

    sync_rows = [
        [
            _format_int(sync.batches_processed),
            _format_int(sync.rows_refined),
            _format_int(sync.rows_stamped),
        ]
    ]
    lines.extend(
        _render_table(
            ["Batches Processed", "Rows Refined", "Rows Stamped"],
            sync_rows,
        )
    )
    lines.append(f"Data source: {sync.source_label}")

    lines.extend(["", "## 3. Pipeline Health Checklist", ""])
    health_table_rows = [
        [flag.provider, flag.severity, _format_int(flag.rows_today), flag.message]
        for flag in health_flags
    ]
    lines.extend(_render_table(["Provider", "Status", "Rows", "Notes"], health_table_rows))
    lines.append("")
    return "\n".join(lines)


def generate_report(
    *,
    report_date: date | None = None,
    timezone_name: str = "Asia/Tehran",
    sync_log_table: str = "",
) -> str:
    tz = ZoneInfo(timezone_name)
    resolved_date = report_date or datetime.now(tz).date()
    window = _report_window(resolved_date, timezone_name)

    with SessionLocal() as session:
        volumetrics = _fetch_provider_volumetrics(session, window)
        discovered = _discover_sync_log_table(session, sync_log_table)
        if discovered:
            sync = _fetch_sync_from_log_table(session, discovered, window)
        else:
            sync = _fetch_sync_proxy(session, window)

    health_flags = _build_health_flags(volumetrics)
    return _render_report(window, volumetrics, sync, health_flags)


def main() -> int:
    args = _build_parser().parse_args()
    try:
        report_date = _parse_report_date(args.date, ZoneInfo(args.timezone)) if args.date else None
        report = generate_report(
            report_date=report_date,
            timezone_name=args.timezone,
            sync_log_table=args.sync_log_table,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
