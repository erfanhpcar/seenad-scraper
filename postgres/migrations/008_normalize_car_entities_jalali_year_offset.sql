-- Bama calculator scrape may store Jalali years with a +1300 offset (e.g. 2645 instead of 1345).
-- PRD "سال ساخت" filters expect standard 4-digit Jalali years (~1300–1450 range for road cars).
-- Apply -1300 only to values that clearly use the wrong scale (>= 2500).

BEGIN;

UPDATE car_entities
SET production_year_start = production_year_start - 1300
WHERE production_year_start IS NOT NULL
  AND production_year_start >= 2500;

UPDATE car_entities
SET production_year_end = production_year_end - 1300
WHERE production_year_end IS NOT NULL
  AND production_year_end >= 2500;

COMMIT;
