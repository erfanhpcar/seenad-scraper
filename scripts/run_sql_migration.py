#!/usr/bin/env python3
"""
Execute a PostgreSQL migration file (semicolon-separated statements) using the app DB config.

Uses the same connection settings as the backend (``DATABASE_URL`` or ``POSTGRES_*`` from
``.env`` / container env). No interactive password prompt; credentials come from the environment.

Examples::

    # From host (repo root), backend service has DATABASE_URL:
    docker compose exec backend python /app/scripts/run_sql_migration.py \\
        /app/postgres/migrations/013_car_zero_price_drop_bama_unique.sql

    # Non-interactive psql on the db container (set PGPASSWORD in the same shell):
    docker compose exec db env PGPASSWORD=\"$POSTGRES_PASSWORD\" psql -U \"${POSTGRES_USER}\" \\
        -d \"${POSTGRES_DB}\" -v ON_ERROR_STOP=1 \\
        -f /var/www/seenad/postgres/migrations/013_car_zero_price_drop_bama_unique.sql
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import engine  # noqa: E402


def _split_sql_statements(sql: str) -> list[str]:
    """Strip full-line SQL comments then split on semicolons."""

    stripped = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    parts: list[str] = []
    for chunk in stripped.split(";"):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sql_path",
        type=Path,
        nargs="?",
        default=Path("postgres/migrations/013_car_zero_price_drop_bama_unique.sql"),
        help="SQL file relative to cwd or absolute (default: drop bama_car_price unique).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    path = args.sql_path.resolve()
    if not path.is_file():
        print(f"migration file not found: {path}", file=sys.stderr)
        return 1
    statements = _split_sql_statements(path.read_text(encoding="utf-8"))
    if not statements:
        print(f"no executable statements in {path}", file=sys.stderr)
        return 1
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    print(f"applied {len(statements)} statement(s) from {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
