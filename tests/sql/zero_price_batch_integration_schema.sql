-- Minimal schema for zero_price batch registry integration tests only.
-- Does not alter operational tables (e.g. car_zero_price). Not used in production.

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

CREATE UNIQUE INDEX IF NOT EXISTS uq_car_zero_price_scrape_snapshot_batch_bama_id
    ON car_zero_price_scrape_snapshot (batch_id, bama_car_price_id)
    WHERE bama_car_price_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS iranjib_zero_price_scrape_snapshot (
    id                     BIGSERIAL PRIMARY KEY,
    batch_id               VARCHAR(40) NOT NULL,
    page_kind              VARCHAR(20) NOT NULL,
    page_url               TEXT NOT NULL,
    section_raw            TEXT NOT NULL DEFAULT '',
    title_raw              TEXT NOT NULL DEFAULT '',
    iranjib_iid            BIGINT NULL,
    market_price_raw       TEXT NOT NULL DEFAULT '',
    factory_price_raw      TEXT NOT NULL DEFAULT '',
    change_amount_raw      TEXT NOT NULL DEFAULT '',
    change_percent_raw     TEXT NOT NULL DEFAULT '',
    status_raw             TEXT NOT NULL DEFAULT '',
    status                 VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    market_price_toman     BIGINT NULL,
    factory_price_toman    BIGINT NULL,
    raw_json               JSONB NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_iranjib_zero_price_scrape_snapshot_batch_len
        CHECK (char_length(batch_id) >= 8)
);

CREATE INDEX IF NOT EXISTS ix_iranjib_zero_price_scrape_snapshot_batch_id
    ON iranjib_zero_price_scrape_snapshot (batch_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_iranjib_zero_price_scrape_snapshot_batch_iid
    ON iranjib_zero_price_scrape_snapshot (batch_id, iranjib_iid)
    WHERE iranjib_iid IS NOT NULL;

CREATE TABLE IF NOT EXISTS zero_price_scrape_batch (
    batch_id       VARCHAR(40) PRIMARY KEY,
    source         VARCHAR(16) NOT NULL,
    status         VARCHAR(16) NOT NULL,
    raw_row_count  INTEGER NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_zero_price_scrape_batch_source
        CHECK (source IN ('bama', 'iranjib')),
    CONSTRAINT ck_zero_price_scrape_batch_status
        CHECK (status IN ('in_progress', 'completed', 'failed')),
    CONSTRAINT ck_zero_price_scrape_batch_batch_len
        CHECK (char_length(batch_id) >= 8)
);

CREATE INDEX IF NOT EXISTS ix_zero_price_scrape_batch_source_status_completed
    ON zero_price_scrape_batch (source, status, completed_at DESC NULLS LAST);
