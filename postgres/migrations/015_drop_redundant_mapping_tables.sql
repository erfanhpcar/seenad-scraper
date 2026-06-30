-- Phase C: remove catalog / mapping tables from Scraper Server (Main Server owns mapping).
-- Keeps: scraped_listings, car_zero_price_scrape_snapshot.
-- Apply: docker compose exec -T backend python /app/scripts/run_sql_migration.py \
--   /app/postgres/migrations/015_drop_redundant_mapping_tables.sql

BEGIN;

DROP TABLE IF EXISTS listings_clean CASCADE;

DROP TABLE IF EXISTS car_zero_price CASCADE;

DROP TABLE IF EXISTS mapping_provider_data CASCADE;

DROP TABLE IF EXISTS mapping_pipeline_history CASCADE;

ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_car_reference_id_fkey;

ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_color_id_fkey;

ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_gearbox_id_fkey;

DROP INDEX IF EXISTS ix_scraped_listings_car_reference_id;

DROP INDEX IF EXISTS ix_scraped_listings_color_id;

DROP INDEX IF EXISTS ix_scraped_listings_gearbox_id;

ALTER TABLE scraped_listings
    DROP COLUMN IF EXISTS car_reference_id;

ALTER TABLE scraped_listings
    DROP COLUMN IF EXISTS color_id;

ALTER TABLE scraped_listings
    DROP COLUMN IF EXISTS gearbox_id;

DROP TABLE IF EXISTS colors CASCADE;

DROP TABLE IF EXISTS gearbox_types CASCADE;

DROP TABLE IF EXISTS car_entities CASCADE;

COMMIT;
