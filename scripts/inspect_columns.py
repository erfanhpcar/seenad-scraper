from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.external_db import external_db_connection, retry_mssql_operation

TARGET_TABLES = ["Divar1", "TELLS", "Divar_j4_900_150"]


def _safe_table_name(table_name: str) -> str:
    return f"[{table_name.replace(']', ']]')}]"


def _print_rows(table_name: str, rows: list[dict[str, Any]]) -> None:
    print(f"Top 5 rows from {table_name}:")
    if not rows:
        print("(no rows)")
        print()
        return
    for index, row in enumerate(rows, start=1):
        print(f"{index}. {json.dumps(row, ensure_ascii=False, default=str)}")
    print()


def main() -> None:
    required_vars = ("EXT_DB_HOST", "EXT_DB_USER", "EXT_DB_PASS", "EXT_DB_NAME")
    missing = [key for key in required_vars if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    def _inspect() -> None:
        with external_db_connection() as conn:
            for table_name in TARGET_TABLES:
                print("=" * 80)
                print(f"Table: {table_name}")
                print("- Columns:")
                columns = conn.execute(
                    text(
                        """
                        SELECT COLUMN_NAME, DATA_TYPE
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_NAME = :table_name
                        ORDER BY ORDINAL_POSITION
                        """
                    ),
                    {"table_name": table_name},
                ).mappings()
                column_rows = list(columns)
                if not column_rows:
                    print("  (table not found)")
                    print()
                    continue
                for column in column_rows:
                    print(f"  - {column['COLUMN_NAME']}: {column['DATA_TYPE']}")
                print()

                safe_table_name = _safe_table_name(table_name)
                rows = conn.execute(text(f"SELECT TOP 5 * FROM {safe_table_name}")).mappings()
                _print_rows(table_name, [dict(row) for row in rows])

    retry_mssql_operation(_inspect, operation_name="inspect_columns")


if __name__ == "__main__":
    main()
