-- Daily sell-condition / بخشنامه crawler (khodro45, car.ir, khodrobank).
-- One article row in car_sell_news; zero or more vehicle rows in car_sell_news_cars.
-- Seenad reads these tables after the 11:00 Asia/Tehran scrape (target pickup 11:15).
-- Do not store impact polarity here; that belongs to the Seenad product.

CREATE TABLE IF NOT EXISTS car_sell_news (
    id                 BIGSERIAL PRIMARY KEY,
    source             TEXT NOT NULL,
    source_article_id  TEXT NULL,
    canonical_url      TEXT NOT NULL,
    source_list_url    TEXT NULL,
    category           TEXT NOT NULL DEFAULT 'بخشنامه',
    title              TEXT NOT NULL,
    subtitle           TEXT NULL,
    full_text          TEXT NULL,
    full_html          TEXT NULL,
    published_at       TIMESTAMPTZ NULL,
    modified_at        TIMESTAMPTZ NULL,
    image_url          TEXT NULL,
    company_name_raw   TEXT NULL,
    sale_type          TEXT NULL,
    sale_type_raw      TEXT NULL,
    campaign_badge     TEXT NULL,
    matched_keywords   TEXT[] NULL,
    raw_json           JSONB NULL,
    parse_ok           BOOLEAN NOT NULL DEFAULT FALSE,
    parse_error        TEXT NULL,
    http_status        INTEGER NULL,
    crawled_at         TIMESTAMPTZ NOT NULL,
    scrape_batch_id    TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_car_sell_news_canonical_url UNIQUE (canonical_url),
    CONSTRAINT ck_car_sell_news_source CHECK (
        source IN ('khodro45', 'car.ir', 'khodrobank')
    )
);

CREATE TABLE IF NOT EXISTS car_sell_news_cars (
    id                 BIGSERIAL PRIMARY KEY,
    news_id            BIGINT NOT NULL REFERENCES car_sell_news(id) ON DELETE CASCADE,
    row_index          INTEGER NOT NULL,
    brand_raw          TEXT NULL,
    model_raw          TEXT NULL,
    trim_raw           TEXT NULL,
    colors_raw         TEXT NULL,
    price_raw          TEXT NULL,
    price_toman        BIGINT NULL,
    price_unit_guess   TEXT NULL,
    plan_name_raw      TEXT NULL,
    delivery_raw       TEXT NULL,
    extra_json         JSONB NULL,
    extract_method     TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_car_sell_news_cars_news_id_row_index UNIQUE (news_id, row_index),
    CONSTRAINT ck_car_sell_news_cars_extract_method CHECK (
        extract_method IN ('html_table', 'key_value_table', 'prose_fallback')
    )
);

CREATE INDEX IF NOT EXISTS ix_car_sell_news_scrape_batch_id
    ON car_sell_news (scrape_batch_id);

CREATE INDEX IF NOT EXISTS ix_car_sell_news_published_at_desc
    ON car_sell_news (published_at DESC);

CREATE INDEX IF NOT EXISTS ix_car_sell_news_source
    ON car_sell_news (source);

CREATE INDEX IF NOT EXISTS ix_car_sell_news_crawled_at
    ON car_sell_news (crawled_at);

CREATE INDEX IF NOT EXISTS ix_car_sell_news_cars_news_id
    ON car_sell_news_cars (news_id);

COMMENT ON TABLE car_sell_news IS
    'One row per sell-condition / بخشنامه article. Unique key: normalized canonical_url.';
COMMENT ON TABLE car_sell_news_cars IS
    'Vehicle rows extracted from a sell-news article (HTML table first, prose fallback).';
COMMENT ON COLUMN car_sell_news.scrape_batch_id IS
    'Daily batch stamp, e.g. sell-news-1405-05-24 (Jalali Asia/Tehran date).';
COMMENT ON COLUMN car_sell_news.parse_ok IS
    'True only when title, canonical_url, and published_at are all present.';
