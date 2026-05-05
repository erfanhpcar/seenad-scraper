from __future__ import annotations

import csv
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import engine

CORE_TABLES = {
    "scraped_listings",
    "listings_clean",
    "mapping_pipeline_history",
}


def _is_scraping_mapping_related(table_name: str) -> bool:
    keywords = ("scrap", "listing", "mapping", "normalize", "pipeline", "clean")
    return any(keyword in table_name.lower() for keyword in keywords)


def export_schema_to_csv(output_path: Path) -> int:
    query = text(
        """
        SELECT
            table_name,
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )

    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()

    filtered_rows = [
        row
        for row in rows
        if row["table_name"] in CORE_TABLES or _is_scraping_mapping_related(row["table_name"])
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=[
                "table_name",
                "column_name",
                "data_type",
                "is_nullable",
                "column_default",
            ],
        )
        writer.writeheader()
        for row in filtered_rows:
            writer.writerow(
                {
                    "table_name": row["table_name"],
                    "column_name": row["column_name"],
                    "data_type": row["data_type"],
                    "is_nullable": row["is_nullable"],
                    "column_default": row["column_default"],
                }
            )

    return len(filtered_rows)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    output_path = project_root / "exports" / "seenad_db_schema.csv"
    row_count = export_schema_to_csv(output_path)
    print(f"Exported {row_count} schema rows to: {output_path}")


if __name__ == "__main__":
    main()
