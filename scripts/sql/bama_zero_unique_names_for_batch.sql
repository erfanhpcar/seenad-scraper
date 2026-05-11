-- Unique raw names from one full crawl batch (for pre-mapping coverage vs car_entities).
--
-- Step 1: set the batch UUID from the scraper log (field batch_id in the summary).
\set batch_id 'REPLACE_WITH_BATCH_UUID_FROM_SCRAPER_LOG'

-- Step 2: run via stdin (paths are examples; run from repo root on the host):
--   docker exec -i seenad_db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--     < scripts/sql/bama_zero_unique_names_for_batch.sql

SELECT DISTINCT
    brand_name_raw AS brand,
    model_name_raw AS model,
    trim_name_raw AS trim,
    model_year_raw AS production_year_raw
FROM car_zero_price_scrape_snapshot
WHERE batch_id = :'batch_id'
ORDER BY 1, 2, 3, 4;

SELECT DISTINCT
    trim(
        both ' '
        FROM concat_ws(
            ' | ',
            NULLIF(brand_name_raw, ''),
            NULLIF(model_name_raw, ''),
            NULLIF(trim_name_raw, ''),
            NULLIF(model_year_raw, '')
        )
    ) AS unique_display_name
FROM car_zero_price_scrape_snapshot
WHERE batch_id = :'batch_id'
ORDER BY 1;
