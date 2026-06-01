from __future__ import annotations

import os

from sqlalchemy import text

from app.external_db import (
    external_db_connection,
    get_external_db_settings,
    ping_external_db,
    retry_mssql_operation,
)


class ExternalDbInspector:
    """Thin wrapper around ``app.external_db`` for scripts and legacy call sites."""

    def __init__(self) -> None:
        settings = get_external_db_settings()
        self.host = settings.host or os.getenv("EXT_DB_HOST", "")
        self.user = settings.user or os.getenv("EXT_DB_USER", "")
        self.password = settings.password or os.getenv("EXT_DB_PASS", "")
        self.database = settings.database or os.getenv("EXT_DB_NAME", "")
        self.port = str(settings.port if settings.port else os.getenv("EXT_DB_PORT", "1433"))

    def _connect(self):
        """
        Borrow a pooled SQLAlchemy connection (caller must close / use as context).

        Prefer ``external_db_connection()`` or ``fetch_table_rows()`` for new code.
        """
        from app.external_db import get_external_db_engine

        get_external_db_settings().validate()
        return get_external_db_engine().connect()

    def ping_connection(self) -> dict[str, object]:
        """
        Verify SQL Server (Divar external DB) via the shared pool.
        Returns a small dict suitable for Celery / monitoring (no secrets).
        """
        return ping_external_db()

    def list_all_tables(self) -> list[str]:
        query = text("SELECT name FROM sys.tables ORDER BY name;")

        def _run() -> list[str]:
            with external_db_connection() as conn:
                rows = conn.execute(query).fetchall()
                return [row[0] for row in rows]

        tables = retry_mssql_operation(_run, operation_name="list_all_tables")

        print("Tables found:")
        for table_name in tables:
            print(f"- {table_name}")

        return tables
