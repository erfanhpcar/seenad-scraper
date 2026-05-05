-- Ensure btree index on scraped_listings.car_reference_id for joins and relink UPDATE plans.
-- FK alone does not create an index on the referencing column in PostgreSQL.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_scraped_listings_car_reference_id
  ON scraped_listings (car_reference_id);

COMMIT;
