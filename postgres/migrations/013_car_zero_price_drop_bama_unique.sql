-- Allow multiple car_entities rows to reference the same Bama catalogue price id when
-- the fuzzy pipeline maps different taxonomy rows similarly, or when API ids repeat.
-- Primary key remains car_reference_id (one price row per entity).

ALTER TABLE car_zero_price DROP CONSTRAINT IF EXISTS car_zero_price_bama_car_price_id_key;

CREATE INDEX IF NOT EXISTS ix_car_zero_price_bama_car_price_id ON car_zero_price (bama_car_price_id);
