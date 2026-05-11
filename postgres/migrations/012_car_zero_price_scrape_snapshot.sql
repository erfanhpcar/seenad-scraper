-- Full-hierarchy raw rows from bama.ir /cad/api/price/hierarchy (for fuzzy pre-mapping).
-- One row per trim line per scrape batch; complements car_zero_price (mapped upserts only).

CREATE TABLE IF NOT EXISTS car_zero_price_scrape_snapshot (
    id                     BIGSERIAL PRIMARY KEY,
    batch_id               VARCHAR(40) NOT NULL,
    hierarchy_page_index   INTEGER NOT NULL,
    brand_name_raw         TEXT NOT NULL DEFAULT '',
    brand_slug_raw         TEXT NOT NULL DEFAULT '',
    model_name_raw         TEXT NOT NULL DEFAULT '',
    model_slug_raw         TEXT NOT NULL DEFAULT '',
    trim_name_raw          TEXT NOT NULL DEFAULT '',
    trim_slug_raw          TEXT NOT NULL DEFAULT '',
    model_year_raw         TEXT NOT NULL DEFAULT '',
    price_raw              TEXT NOT NULL DEFAULT '',
    price_provider         VARCHAR(120) NULL,
    bama_car_price_id      BIGINT NULL,
    raw_json               JSONB NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_car_zero_price_scrape_snapshot_batch_len CHECK (char_length(batch_id) >= 8)
);

CREATE INDEX IF NOT EXISTS ix_car_zero_price_scrape_snapshot_batch_id
    ON car_zero_price_scrape_snapshot (batch_id);

CREATE INDEX IF NOT EXISTS ix_car_zero_price_scrape_snapshot_bama_car_price_id
    ON car_zero_price_scrape_snapshot (bama_car_price_id)
    WHERE bama_car_price_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_car_zero_price_scrape_snapshot_batch_bama_id
    ON car_zero_price_scrape_snapshot (batch_id, bama_car_price_id)
    WHERE bama_car_price_id IS NOT NULL;

COMMENT ON TABLE car_zero_price_scrape_snapshot IS
    'Raw string snapshot of Bama zero-price hierarchy rows; batch_id groups one full crawl run.';

-- Track which hierarchy crawl last refreshed each mapped car_zero_price row (optional).
ALTER TABLE car_zero_price
    ADD COLUMN IF NOT EXISTS scrape_batch_id VARCHAR(40) NULL;

CREATE INDEX IF NOT EXISTS ix_car_zero_price_scrape_batch_id
    ON car_zero_price (scrape_batch_id)
    WHERE scrape_batch_id IS NOT NULL;
