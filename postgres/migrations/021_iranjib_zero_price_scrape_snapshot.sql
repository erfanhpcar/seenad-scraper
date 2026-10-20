-- Raw market-price rows from iranjib.ir domestic + imported catalogue pages.
-- One row per table line per scrape batch. Entity matching belongs on the Main Server.

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
        CHECK (char_length(batch_id) >= 8),
    CONSTRAINT ck_iranjib_zero_price_scrape_snapshot_page_kind
        CHECK (page_kind IN ('domestic', 'imported')),
    CONSTRAINT ck_iranjib_zero_price_scrape_snapshot_status
        CHECK (status IN ('AVAILABLE', 'UNAVAILABLE', 'STOPPED', 'COMING_SOON', 'UNKNOWN'))
);

CREATE INDEX IF NOT EXISTS ix_iranjib_zero_price_scrape_snapshot_batch_id
    ON iranjib_zero_price_scrape_snapshot (batch_id);

CREATE INDEX IF NOT EXISTS ix_iranjib_zero_price_scrape_snapshot_page_kind
    ON iranjib_zero_price_scrape_snapshot (page_kind);

CREATE INDEX IF NOT EXISTS ix_iranjib_zero_price_scrape_snapshot_created_at
    ON iranjib_zero_price_scrape_snapshot (created_at);

CREATE INDEX IF NOT EXISTS ix_iranjib_zero_price_scrape_snapshot_iid
    ON iranjib_zero_price_scrape_snapshot (iranjib_iid)
    WHERE iranjib_iid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_iranjib_zero_price_scrape_snapshot_batch_iid
    ON iranjib_zero_price_scrape_snapshot (batch_id, iranjib_iid)
    WHERE iranjib_iid IS NOT NULL;

COMMENT ON TABLE iranjib_zero_price_scrape_snapshot IS
    'Raw snapshot of IranJib zero-km market prices. batch_id groups one full crawl of domestic+imported.';

COMMENT ON COLUMN iranjib_zero_price_scrape_snapshot.iranjib_iid IS
    'Stable IranJib row id from getchart.php iid / data-target. Survives display-name changes.';

COMMENT ON COLUMN iranjib_zero_price_scrape_snapshot.market_price_toman IS
    'Parsed market price as displayed (toman). NULL for sentinels such as unavailable.';

COMMENT ON COLUMN iranjib_zero_price_scrape_snapshot.page_kind IS
    'domestic = /showgroup/45 (تولید داخل), imported = /showgroup/46 (وارداتی).';
