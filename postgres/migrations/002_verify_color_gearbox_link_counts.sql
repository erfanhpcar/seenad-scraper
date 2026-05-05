-- بعد از اجرای مهاجرت SQL و اسکریپت migrate_scraped_listings_color_gearbox.py
-- یک کوئری واحد برای آمار لینک موفق:

SELECT
    COUNT(*) AS total_rows,
    COUNT(*) FILTER (WHERE color_id IS NOT NULL) AS rows_with_color_fk,
    COUNT(*) FILTER (WHERE gearbox_id IS NOT NULL) AS rows_with_gearbox_fk,
    COUNT(*) FILTER (WHERE color_id IS NOT NULL AND gearbox_id IS NOT NULL) AS rows_both_fk_set,
    COUNT(*) FILTER (
        WHERE raw_data->>'color' IS NOT NULL
          AND TRIM(BOTH FROM raw_data->>'color') <> ''
          AND color_id IS NOT NULL
    ) AS rows_color_json_matched,
    COUNT(*) FILTER (
        WHERE raw_data->>'gearbox' IS NOT NULL
          AND TRIM(BOTH FROM raw_data->>'gearbox') <> ''
          AND gearbox_id IS NOT NULL
    ) AS rows_gearbox_json_matched
FROM scraped_listings;
