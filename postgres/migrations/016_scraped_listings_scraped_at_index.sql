-- Speed up retention deletes and age-based queries on scraped_listings.
CREATE INDEX IF NOT EXISTS ix_scraped_listings_scraped_at
    ON scraped_listings (scraped_at);

COMMENT ON INDEX ix_scraped_listings_scraped_at IS
    'Supports nightly retention purge (scraped_at < NOW() - retention window).';
