-- Replace car_reference with car_entities (PRD: brand, model, trim, production years).
-- Clears listing links so they can be remapped after the taxonomy refresh.

-- mapping_provider_data (created by tooling scripts) may FK to car_reference; drop before DROP TABLE.
ALTER TABLE IF EXISTS mapping_provider_data
  DROP CONSTRAINT IF EXISTS mapping_provider_data_target_reference_id_fkey;

ALTER TABLE scraped_listings
  DROP CONSTRAINT IF EXISTS scraped_listings_car_reference_id_fkey;

ALTER TABLE listings_clean
  DROP CONSTRAINT IF EXISTS listings_clean_car_reference_id_fkey;

DROP TABLE IF EXISTS car_reference;
DROP TABLE IF EXISTS car_entities;

CREATE TABLE car_entities (
  id                  SERIAL PRIMARY KEY,
  brand               VARCHAR(100) NOT NULL,
  model               VARCHAR(120) NOT NULL,
  trim                VARCHAR(160) NOT NULL,
  production_year_start INTEGER NULL,
  production_year_end   INTEGER NULL,
  source_url          VARCHAR(300) NOT NULL DEFAULT 'https://bama.ir/calculator',
  CONSTRAINT uq_car_entities_brand_model_trim UNIQUE (brand, model, trim)
);

CREATE INDEX IF NOT EXISTS ix_car_entities_brand ON car_entities (brand);
CREATE INDEX IF NOT EXISTS ix_car_entities_model ON car_entities (model);

UPDATE scraped_listings SET car_reference_id = NULL WHERE car_reference_id IS NOT NULL;
UPDATE listings_clean SET car_reference_id = NULL WHERE car_reference_id IS NOT NULL;

ALTER TABLE scraped_listings
  ADD CONSTRAINT scraped_listings_car_reference_id_fkey
  FOREIGN KEY (car_reference_id) REFERENCES car_entities (id) ON DELETE SET NULL;

ALTER TABLE listings_clean
  ADD CONSTRAINT listings_clean_car_reference_id_fkey
  FOREIGN KEY (car_reference_id) REFERENCES car_entities (id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_scraped_listings_car_reference_id
  ON scraped_listings (car_reference_id);

CREATE INDEX IF NOT EXISTS ix_listings_clean_car_reference_id
  ON listings_clean (car_reference_id);

-- Old car_reference IDs are invalid after taxonomy rebuild; clear before re-adding FK.
DO $$
BEGIN
  IF to_regclass('public.mapping_provider_data') IS NOT NULL THEN
    UPDATE mapping_provider_data
    SET target_reference_id = NULL
    WHERE target_reference_id IS NOT NULL;
  END IF;
END $$;

ALTER TABLE IF EXISTS mapping_provider_data
  ADD CONSTRAINT mapping_provider_data_target_reference_id_fkey
  FOREIGN KEY (target_reference_id) REFERENCES car_entities (id) ON DELETE SET NULL;
