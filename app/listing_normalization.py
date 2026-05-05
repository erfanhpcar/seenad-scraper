"""Map raw color / gearbox strings from scraped_listings.raw_data to reference IDs."""

from __future__ import annotations

from sqlalchemy import text

from app.database import SessionLocal


def run_color_gearbox_normalization() -> tuple[int, int]:
    """
    Set color_id and gearbox_id where raw_data has values and FKs are still NULL.
    Matching: TRIM(raw_data->>'color') = colors.name (same for gearbox / gearbox_types).
    """
    color_sql = text(
        """
        UPDATE scraped_listings AS sl
        SET color_id = c.id
        FROM colors AS c
        WHERE TRIM(BOTH FROM sl.raw_data->>'color') = c.name
          AND sl.raw_data->>'color' IS NOT NULL
          AND TRIM(BOTH FROM sl.raw_data->>'color') <> ''
          AND sl.color_id IS NULL
        """
    )
    gearbox_sql = text(
        """
        UPDATE scraped_listings AS sl
        SET gearbox_id = g.id
        FROM gearbox_types AS g
        WHERE TRIM(BOTH FROM sl.raw_data->>'gearbox') = g.name
          AND sl.raw_data->>'gearbox' IS NOT NULL
          AND TRIM(BOTH FROM sl.raw_data->>'gearbox') <> ''
          AND sl.gearbox_id IS NULL
        """
    )
    with SessionLocal() as session:
        color_rows = session.execute(color_sql)
        gear_rows = session.execute(gearbox_sql)
        session.commit()
        return int(color_rows.rowcount or 0), int(gear_rows.rowcount or 0)
