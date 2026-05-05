from __future__ import annotations

import os

import pymssql


class ExternalDbInspector:
    def __init__(self) -> None:
        self.host = os.getenv("EXT_DB_HOST", "")
        self.user = os.getenv("EXT_DB_USER", "")
        self.password = os.getenv("EXT_DB_PASS", "")
        self.database = os.getenv("EXT_DB_NAME", "")
        self.port = os.getenv("EXT_DB_PORT", "1433")

    def _connect(self) -> pymssql.Connection:
        if not self.host or not self.user or not self.password or not self.database:
            raise RuntimeError(
                "Missing external DB config. Set EXT_DB_HOST, EXT_DB_USER, EXT_DB_PASS, EXT_DB_NAME."
            )
        return pymssql.connect(
            server=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=int(self.port),
            login_timeout=15,
            timeout=30,
        )

    def ping_connection(self) -> dict[str, object]:
        """
        Open one connection to SQL Server (Divar external DB) and close it.
        Returns a small dict suitable for Celery / monitoring (no secrets).
        """
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                conn.close()
            return {"ok": True, "host": self.host, "database": self.database, "port": int(self.port)}
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "host": self.host or None,
                "database": self.database or None,
                "port": int(self.port) if str(self.port).isdigit() else self.port,
            }

    def list_all_tables(self) -> list[str]:
        query = "SELECT name FROM sys.tables ORDER BY name;"
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            tables = [row[0] for row in rows]
        finally:
            connection.close()

        print("Tables found:")
        for table_name in tables:
            print(f"- {table_name}")

        return tables
