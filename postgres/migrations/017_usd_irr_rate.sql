-- Daily free-market USD/IRR rate (TGJU price_dollar_rl) for Main Server car-price conversion.
-- One row per Tehran calendar day per source; re-runs the same day upsert.

CREATE TABLE IF NOT EXISTS usd_irr_rate (
    id               BIGSERIAL PRIMARY KEY,
    as_of_date       DATE NOT NULL,
    rate_irr         BIGINT NOT NULL,
    rate_toman       BIGINT NOT NULL,
    source           VARCHAR(50) NOT NULL DEFAULT 'tgju',
    instrument_key   VARCHAR(80) NOT NULL DEFAULT 'price_dollar_rl',
    quoted_at        TIMESTAMPTZ NULL,
    high_irr         BIGINT NULL,
    low_irr          BIGINT NULL,
    change_irr       BIGINT NULL,
    change_percent   NUMERIC(12, 4) NULL,
    raw_json         JSONB NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_usd_irr_rate_rate_irr_positive CHECK (rate_irr > 0),
    CONSTRAINT ck_usd_irr_rate_rate_toman_positive CHECK (rate_toman > 0),
    CONSTRAINT uq_usd_irr_rate_as_of_date_source UNIQUE (as_of_date, source)
);

CREATE INDEX IF NOT EXISTS ix_usd_irr_rate_as_of_date
    ON usd_irr_rate (as_of_date DESC);

CREATE INDEX IF NOT EXISTS ix_usd_irr_rate_source
    ON usd_irr_rate (source);

COMMENT ON TABLE usd_irr_rate IS
    'Daily free-market USD to IRR (not official/CBI). Primary source: TGJU price_dollar_rl.';

COMMENT ON COLUMN usd_irr_rate.rate_irr IS
    'Iranian Rials per 1 USD (free market).';

COMMENT ON COLUMN usd_irr_rate.rate_toman IS
    'Iranian Tomans per 1 USD (= rate_irr / 10).';

COMMENT ON COLUMN usd_irr_rate.as_of_date IS
    'Tehran calendar date this snapshot represents.';
