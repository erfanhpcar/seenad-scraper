-- Ensure historical snapshot rows carry an insertion timestamp on older databases.

ALTER TABLE car_zero_price_scrape_snapshot
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS ix_car_zero_price_scrape_snapshot_created_at
    ON car_zero_price_scrape_snapshot (created_at);

COMMENT ON COLUMN car_zero_price_scrape_snapshot.created_at IS
    'Timestamp when this raw Bama zero-price hierarchy row was inserted.';
