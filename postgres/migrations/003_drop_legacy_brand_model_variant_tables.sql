-- Remove legacy taxonomy (brands / car_models / car_variants) in favor of car_reference.
-- Prerequisite: application code must no longer reference these tables (see models.py).

ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_car_variant_id_fkey;

ALTER TABLE scraped_listings
    DROP COLUMN IF EXISTS car_variant_id;

DROP TABLE IF EXISTS car_variants CASCADE;
DROP TABLE IF EXISTS car_models CASCADE;
DROP TABLE IF EXISTS brands CASCADE;
