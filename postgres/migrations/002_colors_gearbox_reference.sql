-- Reference tables for color and gearbox (normalized from scraped_listings.raw_data JSONB).
-- Run: psql -v ON_ERROR_STOP=1 -f postgres/migrations/002_colors_gearbox_reference.sql
--
-- Steps: (1) create colors + gearbox_types, (2) seed distinct values from raw_data,
--        (3) add color_id / gearbox_id on scraped_listings with FKs ON DELETE SET NULL.
-- Data backfill: run scripts/migrate_scraped_listings_color_gearbox.py after this file.

BEGIN;

-- 1) Reference tables
CREATE TABLE IF NOT EXISTS colors (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS gearbox_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE
);

-- 2) Seed from distinct JSON keys (ignore null, empty, whitespace-only)
INSERT INTO colors (name)
SELECT DISTINCT TRIM(BOTH FROM sub.v) AS name
FROM (
    SELECT raw_data->>'color' AS v
    FROM scraped_listings
) AS sub
WHERE sub.v IS NOT NULL
  AND TRIM(BOTH FROM sub.v) <> ''
ON CONFLICT (name) DO NOTHING;

INSERT INTO gearbox_types (name)
SELECT DISTINCT TRIM(BOTH FROM sub.v) AS name
FROM (
    SELECT raw_data->>'gearbox' AS v
    FROM scraped_listings
) AS sub
WHERE sub.v IS NOT NULL
  AND TRIM(BOTH FROM sub.v) <> ''
ON CONFLICT (name) DO NOTHING;

-- 3) Nullable FK columns on main table
ALTER TABLE scraped_listings
    ADD COLUMN IF NOT EXISTS color_id INTEGER;

ALTER TABLE scraped_listings
    ADD COLUMN IF NOT EXISTS gearbox_id INTEGER;

-- Drop old FK names if re-running migration
ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_color_id_fkey;

ALTER TABLE scraped_listings
    DROP CONSTRAINT IF EXISTS scraped_listings_gearbox_id_fkey;

ALTER TABLE scraped_listings
    ADD CONSTRAINT scraped_listings_color_id_fkey
    FOREIGN KEY (color_id)
    REFERENCES colors (id)
    ON DELETE SET NULL;

ALTER TABLE scraped_listings
    ADD CONSTRAINT scraped_listings_gearbox_id_fkey
    FOREIGN KEY (gearbox_id)
    REFERENCES gearbox_types (id)
    ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_scraped_listings_color_id ON scraped_listings (color_id);
CREATE INDEX IF NOT EXISTS ix_scraped_listings_gearbox_id ON scraped_listings (gearbox_id);

COMMIT;

-- 5) Verification (run after Python backfill):
-- SELECT COUNT(*) AS rows_both_linked
-- FROM scraped_listings
-- WHERE color_id IS NOT NULL AND gearbox_id IS NOT NULL;
