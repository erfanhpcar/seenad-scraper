"""Upsert listings_clean from scraped_listings + resolved color / gearbox names."""

from __future__ import annotations

from sqlalchemy import text

from app.database import SessionLocal

_SYNC_SQL = text(
    """
    INSERT INTO listings_clean (
      scraped_listing_id,
      brand_name,
      model_name,
      model_year,
      price,
      mileage,
      color_name,
      gearbox_name,
      car_reference_id,
      updated_at
    )
    SELECT
      sl.id,
      sl.brand_name,
      sl.model_name,
      CASE
        WHEN sl.raw_data->'normalized'->>'model_year' IS NOT NULL
             AND trim(sl.raw_data->'normalized'->>'model_year') <> ''
        THEN trim(sl.raw_data->'normalized'->>'model_year')::integer
        ELSE NULL
      END AS model_year,
      sl.price,
      sl.mileage,
      c.name,
      g.name,
      sl.car_reference_id,
      NOW()
    FROM scraped_listings sl
    LEFT JOIN colors c ON c.id = sl.color_id
    LEFT JOIN gearbox_types g ON g.id = sl.gearbox_id
    ON CONFLICT (scraped_listing_id) DO UPDATE SET
      brand_name = EXCLUDED.brand_name,
      model_name = EXCLUDED.model_name,
      model_year = EXCLUDED.model_year,
      price = EXCLUDED.price,
      mileage = EXCLUDED.mileage,
      color_name = EXCLUDED.color_name,
      gearbox_name = EXCLUDED.gearbox_name,
      car_reference_id = EXCLUDED.car_reference_id,
      updated_at = NOW()
    """
)


def sync_listings_clean_from_scraped() -> int:
    """Full refresh of listings_clean from scraped_listings (all sources). Returns rows affected."""
    with SessionLocal() as session:
        result = session.execute(_SYNC_SQL)
        session.commit()
        return int(result.rowcount or 0)
