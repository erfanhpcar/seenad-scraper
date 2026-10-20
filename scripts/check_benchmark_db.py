#!/usr/bin/env python3
"""Verify benchmark Postgres connectivity and table counts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text


def _load_url() -> str:
    secret = Path("secrets/benchmark_database_url")
    if secret.is_file():
        return secret.read_text(encoding="utf-8").strip()
    url = os.getenv("BENCHMARK_DATABASE_URL", "").strip()
    if url:
        return url
    url = os.getenv("DATABASE_URL", "").strip()
    return url


def main() -> int:
    url = _load_url()
    if not url:
        print("Missing BENCHMARK_DATABASE_URL or secrets/benchmark_database_url", file=sys.stderr)
        return 1
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        tables = conn.execute(
            text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'benchmark%'
                ORDER BY 1
                """
            )
        ).scalars().all()
        print("benchmark tables:", tables)
        if "benchmark_car" not in tables:
            print("ERROR: benchmark_car not found — wrong database?", file=sys.stderr)
            return 2
        active = conn.execute(
            text("SELECT count(*) FROM benchmark_car WHERE is_active IS TRUE")
        ).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM benchmark_run")).scalar_one()
        print(f"active benchmark_car: {active}")
        print(f"benchmark_run rows: {runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
