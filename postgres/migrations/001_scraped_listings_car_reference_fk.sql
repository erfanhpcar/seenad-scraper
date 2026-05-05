-- Add FOREIGN KEY: scraped_listings.car_reference_id -> car_reference(id)
-- Run with: psql -v ON_ERROR_STOP=1 -f postgres/migrations/001_scraped_listings_car_reference_fk.sql
--
-- Orphan car_reference_id values (not present in car_reference) are cleared to NULL
-- before the constraint is created. Rows are not deleted.

BEGIN;

-- Optional: see how many rows will be fixed (comment out if not needed)
-- SELECT COUNT(*) AS invalid_car_reference_id_rows
-- FROM scraped_listings sl
-- WHERE sl.car_reference_id IS NOT NULL
--   AND NOT EXISTS (SELECT 1 FROM car_reference cr WHERE cr.id = sl.car_reference_id);

UPDATE scraped_listings AS sl
SET car_reference_id = NULL
WHERE sl.car_reference_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM car_reference AS cr
    WHERE cr.id = sl.car_reference_id
  );

ALTER TABLE scraped_listings
  DROP CONSTRAINT IF EXISTS scraped_listings_car_reference_id_fkey;

ALTER TABLE scraped_listings
  ADD CONSTRAINT scraped_listings_car_reference_id_fkey
  FOREIGN KEY (car_reference_id)
  REFERENCES car_reference (id)
  ON DELETE SET NULL;

COMMIT;
