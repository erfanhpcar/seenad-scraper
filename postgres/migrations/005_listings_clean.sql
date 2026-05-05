-- Denormalized clean listings layer (PRD): one row per scraped_listing after normalization/mapping.

BEGIN;

CREATE TABLE IF NOT EXISTS listings_clean (
  id                  BIGSERIAL PRIMARY KEY,
  scraped_listing_id  BIGINT NOT NULL UNIQUE REFERENCES scraped_listings (id) ON DELETE CASCADE,
  brand_name          VARCHAR(100),
  model_name          VARCHAR(120),
  model_year          INTEGER,
  price               NUMERIC(14, 2),
  mileage             INTEGER,
  color_name          VARCHAR(255),
  gearbox_name        VARCHAR(255),
  car_reference_id    INTEGER REFERENCES car_reference (id) ON DELETE SET NULL,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_listings_clean_car_reference_id
  ON listings_clean (car_reference_id);

CREATE INDEX IF NOT EXISTS ix_listings_clean_brand_model
  ON listings_clean (brand_name, model_name);

COMMENT ON TABLE listings_clean IS 'Denormalized snapshot from scraped_listings + resolved color/gearbox names; refreshed by worker sync task.';

COMMIT;
