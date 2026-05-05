"""
Backfill scraped_listings.color_id and scraped_listings.gearbox_id from raw_data.

Prerequisite: run postgres/migrations/002_colors_gearbox_reference.sql

Matching rule: TRIM(raw_data->>'color') = colors.name (same for gearbox / gearbox_types).
"""
from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


def backfill() -> None:
    with SessionLocal() as session:
        total = session.execute(text("SELECT COUNT(*) FROM scraped_listings")).scalar_one()
        print(f"scraped_listings row count: {total}")

        color_upd = session.execute(
            text(
                """
                UPDATE scraped_listings AS sl
                SET color_id = c.id
                FROM colors AS c
                WHERE TRIM(BOTH FROM sl.raw_data->>'color') = c.name
                  AND sl.raw_data->>'color' IS NOT NULL
                  AND TRIM(BOTH FROM sl.raw_data->>'color') <> ''
                """
            )
        )
        print(f"UPDATE color_id: rowcount {color_upd.rowcount}")

        gear_upd = session.execute(
            text(
                """
                UPDATE scraped_listings AS sl
                SET gearbox_id = g.id
                FROM gearbox_types AS g
                WHERE TRIM(BOTH FROM sl.raw_data->>'gearbox') = g.name
                  AND sl.raw_data->>'gearbox' IS NOT NULL
                  AND TRIM(BOTH FROM sl.raw_data->>'gearbox') <> ''
                """
            )
        )
        print(f"UPDATE gearbox_id: rowcount {gear_upd.rowcount}")

        session.commit()

        linked_both = session.execute(
            text(
                """
                SELECT COUNT(*) FROM scraped_listings
                WHERE color_id IS NOT NULL AND gearbox_id IS NOT NULL
                """
            )
        ).scalar_one()
        print(f"Rows with both color_id and gearbox_id set: {linked_both}")


def main() -> None:
    backfill()
    print("Color/gearbox FK backfill completed.")


if __name__ == "__main__":
    main()
