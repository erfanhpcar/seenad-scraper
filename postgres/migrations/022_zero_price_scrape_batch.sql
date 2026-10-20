-- Completion registry for Bama / IranJib zero-price snapshot batches.
-- seenad-main should read only rows with status = 'completed' (see docs/zero_price_three_daily_schedule.md).

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

COMMENT ON TABLE zero_price_scrape_batch IS
    'Lifecycle marker for zero-price snapshot batches; partial crawler runs stay in_progress/failed.';

COMMENT ON COLUMN zero_price_scrape_batch.status IS
    'completed = safe for seenad-main; in_progress/failed snapshot rows must be ignored.';
