-- Unify Divar pipeline source to literal ''divar'' and namespace external IDs by upstream SQL Server table
-- so (source, external_id) stays unique across Divar1 / TELLS / etc.

BEGIN;

UPDATE scraped_listings AS sl
SET
  source = 'divar',
  external_id = regexp_replace(sl.source, '^divar_external:', '') || ':' || sl.external_id::text,
  raw_data = COALESCE(sl.raw_data, '{}'::jsonb)
    || jsonb_build_object(
         'external_table',
         regexp_replace(sl.source, '^divar_external:', '')
       )
WHERE sl.source LIKE 'divar_external:%';

COMMIT;
