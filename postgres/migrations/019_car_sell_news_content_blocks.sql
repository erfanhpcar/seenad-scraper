-- Structured article body for Seenad sell-news rendering (paragraphs, headings, cars).
-- Additive only: do not rename or drop existing car_sell_news columns.

ALTER TABLE car_sell_news
    ADD COLUMN IF NOT EXISTS content_blocks jsonb,
    ADD COLUMN IF NOT EXISTS content_blocks_version smallint NOT NULL DEFAULT 1;

COMMENT ON COLUMN car_sell_news.content_blocks IS
    'Ordered JSON array of article blocks: heading, paragraph, list, quote, note, cars. No image blocks.';
COMMENT ON COLUMN car_sell_news.content_blocks_version IS
    'Schema version of content_blocks (1 = initial structured body).';
COMMENT ON COLUMN car_sell_news.parse_ok IS
    'True when title and canonical_url are present and the article has a valid text block or at least one car row.';
