"""Upsert listings_clean from scraped_listings + resolved color / gearbox names."""

from __future__ import annotations

from sqlalchemy import text

from app.database import SessionLocal

_SYNC_SQL = text(
    """
    WITH resolved AS (
      SELECT
        sl.id AS scraped_listing_id,
        sl.brand_name,
        sl.model_name,
        CASE
          WHEN sl.model_year IS NOT NULL THEN sl.model_year
          WHEN sl.raw_data->'normalized'->>'model_year' IS NOT NULL
               AND trim(sl.raw_data->'normalized'->>'model_year') ~ '^(13|14)\\d{2}$'
          THEN trim(sl.raw_data->'normalized'->>'model_year')::integer
          ELSE NULL
        END AS model_year,
        sl.price,
        sl.mileage,
        c.name AS color_name,
        g.name AS gearbox_name,
        sl.car_reference_id
      FROM scraped_listings sl
      LEFT JOIN colors c ON c.id = sl.color_id
      LEFT JOIN gearbox_types g ON g.id = sl.gearbox_id
    )
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
      r.scraped_listing_id,
      r.brand_name,
      r.model_name,
      r.model_year,
      r.price,
      r.mileage,
      r.color_name,
      r.gearbox_name,
      r.car_reference_id,
      NOW()
    FROM resolved r
    WHERE r.model_year IS NOT NULL
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

_DELETE_NULL_YEAR_SQL = text(
    """
    DELETE FROM listings_clean lc
    USING scraped_listings sl
    WHERE lc.scraped_listing_id = sl.id
      AND (
        CASE
          WHEN sl.model_year IS NOT NULL THEN sl.model_year
          WHEN sl.raw_data->'normalized'->>'model_year' IS NOT NULL
               AND trim(sl.raw_data->'normalized'->>'model_year') ~ '^(13|14)\\d{2}$'
          THEN trim(sl.raw_data->'normalized'->>'model_year')::integer
          ELSE NULL
        END
      ) IS NULL
    """
)


def sync_listings_clean_from_scraped() -> int:
    """Full refresh of listings_clean from scraped_listings (all sources). Returns rows affected."""
    with SessionLocal() as session:
        session.execute(_DELETE_NULL_YEAR_SQL)
        result = session.execute(_SYNC_SQL)
        session.commit()
        return int(result.rowcount or 0)
