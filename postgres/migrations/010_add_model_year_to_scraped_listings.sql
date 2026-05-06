ALTER TABLE scraped_listings
ADD COLUMN IF NOT EXISTS model_year INTEGER;

CREATE INDEX IF NOT EXISTS ix_scraped_listings_model_year
ON scraped_listings (model_year);
