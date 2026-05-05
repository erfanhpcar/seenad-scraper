BEGIN;

-- Prefer listings_raw if present; otherwise use scraped_listings (current canonical table).
ALTER TABLE IF EXISTS listings_raw
  ADD COLUMN IF NOT EXISTS created_at_source TIMESTAMPTZ NULL;

ALTER TABLE IF EXISTS scraped_listings
  ADD COLUMN IF NOT EXISTS created_at_source TIMESTAMPTZ NULL;

COMMIT;
