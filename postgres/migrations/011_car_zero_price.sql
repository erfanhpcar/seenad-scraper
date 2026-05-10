-- Zero-kilometre (catalog) prices sourced from bama.ir /price (CAD price API).

CREATE TABLE IF NOT EXISTS car_zero_price (
    car_reference_id INTEGER NOT NULL REFERENCES car_entities (id) ON DELETE CASCADE,
    price NUMERIC(14, 2) NOT NULL,
    currency           VARCHAR(16) NOT NULL DEFAULT 'IRR',
    model_year         INTEGER NULL,
    price_provider     VARCHAR(120) NULL,
    bama_car_price_id  BIGINT NULL,
    source             VARCHAR(64) NOT NULL DEFAULT 'bama.ir/price',
    raw_snapshot       JSONB NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_car_zero_price PRIMARY KEY (car_reference_id)
);

CREATE INDEX IF NOT EXISTS ix_car_zero_price_updated_at ON car_zero_price (updated_at);

COMMENT ON TABLE car_zero_price IS 'Latest scraped zero-km catalogue price per car_entities row (Bama CAD price feed).';
