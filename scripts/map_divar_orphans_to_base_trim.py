#!/usr/bin/env python3
"""
Quick coverage lift for Divar orphans with known brand/model but missing trim.

Strategy:
- For each brand/model pair in car_entities, pick a "base/simple" trim if present.
- Otherwise, pick the shortest trim text as a conservative default.
- Relink Divar scraped_listings rows (car_reference_id IS NULL) when trim is empty/generic.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.listings_clean_sync import sync_listings_clean_from_scraped

LOGGER = logging.getLogger("map-divar-orphans-base-trim")

_BRAND_ALIAS_SEEDS: tuple[tuple[str, str], ...] = (
    ("peugeot", "پژو"),
    ("ikco", "ایران خودرو"),
    ("iran khodro", "ایران خودرو"),
    ("saipa", "سایپا"),
    ("jac", "جک"),
    ("renault", "رنو"),
    ("citroen", "سیتروئن"),
)

_MODEL_ALIAS_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("پژو", "207i", "207"),
    ("پژو", "207 آی", "207"),
    ("پژو", "206 تیپ 2", "206"),
    ("پژو", "206 تیپ 5", "206"),
    ("ایران خودرو", "دنا پلاس توربو", "دنا پلاس"),
    ("ایران خودرو", "دنا معمولی", "دنا"),
    ("ایران خودرو", "تارا v1", "تارا"),
    ("ایران خودرو", "تارا v4", "تارا"),
    ("سایپا", "کوییک s", "کوییک"),
    ("سایپا", "کوییک r", "کوییک"),
    ("سایپا", "تیبا 2", "تیبا"),
    ("جک", "j4", "J4"),
    ("جک", "s5", "S5"),
    ("رنو", "l90", "تندر 90"),
)

_SEED_BRAND_ALIASES_SQL = text(
    """
WITH base_brand AS (
  SELECT
    ce.id,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS brand_norm,
    ROW_NUMBER() OVER (
      PARTITION BY lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
      ORDER BY ce.id ASC
    ) AS rn
  FROM car_entities ce
),
seed(alias_raw, canonical_brand_raw) AS (
  VALUES {brand_seed_values}
),
resolved AS (
  SELECT
    lower(btrim(regexp_replace(translate(s.alias_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS alias_norm,
    bb.id AS target_reference_id
  FROM seed s
  INNER JOIN base_brand bb
    ON bb.rn = 1
   AND bb.brand_norm = lower(btrim(regexp_replace(translate(s.canonical_brand_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
)
INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id)
SELECT 'divar_brand_alias', r.alias_norm, r.target_reference_id
FROM resolved r
ON CONFLICT (provider_name, source_string)
DO UPDATE SET
  target_reference_id = EXCLUDED.target_reference_id,
  updated_at = NOW()
"""
)

_SEED_MODEL_ALIASES_SQL = text(
    """
WITH base_trim AS (
  SELECT
    ce.id,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS brand_norm,
    lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS model_norm,
    ROW_NUMBER() OVER (
      PARTITION BY
        lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))),
        lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
      ORDER BY
        CASE
          WHEN lower(btrim(ce.trim)) IN ('پایه', 'ساده', 'base', 'basic', 'std', 'standard') THEN 0
          WHEN ce.trim LIKE '%پایه%' OR ce.trim LIKE '%ساده%' THEN 1
          ELSE 2
        END,
        char_length(btrim(ce.trim)) ASC,
        ce.id ASC
    ) AS rn
  FROM car_entities ce
),
seed(canonical_brand_raw, alias_model_raw, canonical_model_raw) AS (
  VALUES {model_seed_values}
),
resolved AS (
  SELECT
    concat(
      lower(btrim(regexp_replace(translate(s.canonical_brand_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))),
      '|',
      lower(btrim(regexp_replace(translate(s.alias_model_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
    ) AS alias_key,
    bt.id AS target_reference_id
  FROM seed s
  INNER JOIN base_trim bt
    ON bt.rn = 1
   AND bt.brand_norm = lower(btrim(regexp_replace(translate(s.canonical_brand_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
   AND bt.model_norm = lower(btrim(regexp_replace(translate(s.canonical_model_raw, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
)
INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id)
SELECT 'divar_model_alias', r.alias_key, r.target_reference_id
FROM resolved r
ON CONFLICT (provider_name, source_string)
DO UPDATE SET
  target_reference_id = EXCLUDED.target_reference_id,
  updated_at = NOW()
"""
)

_UPDATE_SQL = text(
    """
WITH base_trim AS (
  SELECT
    ce.id,
    lower(
      btrim(
        regexp_replace(
          translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS brand_norm,
    lower(
      btrim(
        regexp_replace(
          translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS model_norm,
    ROW_NUMBER() OVER (
      PARTITION BY
        lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))),
        lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
      ORDER BY
        CASE
          WHEN lower(btrim(ce.trim)) IN ('پایه', 'ساده', 'base', 'basic', 'std', 'standard') THEN 0
          WHEN ce.trim LIKE '%پایه%' OR ce.trim LIKE '%ساده%' THEN 1
          ELSE 2
        END,
        char_length(btrim(ce.trim)) ASC,
        ce.id ASC
    ) AS rn
  FROM car_entities ce
),
brand_alias AS (
  SELECT
    lower(btrim(mpd.source_string)) AS alias_brand_norm,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_brand_norm
  FROM mapping_provider_data mpd
  INNER JOIN car_entities ce ON ce.id = mpd.target_reference_id
  WHERE mpd.provider_name = 'divar_brand_alias'
    AND mpd.target_reference_id IS NOT NULL
),
model_alias AS (
  SELECT
    lower(btrim(mpd.source_string)) AS alias_key,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_brand_norm,
    lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_model_norm
  FROM mapping_provider_data mpd
  INNER JOIN car_entities ce ON ce.id = mpd.target_reference_id
  WHERE mpd.provider_name = 'divar_model_alias'
    AND mpd.target_reference_id IS NOT NULL
),
listing_norm AS (
  SELECT
    sl.id,
    lower(
      btrim(
        regexp_replace(
          translate(sl.brand_name, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS brand_norm,
    lower(
      btrim(
        regexp_replace(
          translate(sl.model_name, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS model_norm
  FROM scraped_listings sl
  WHERE sl.source = 'divar'
    AND sl.car_reference_id IS NULL
    AND sl.brand_name IS NOT NULL
    AND sl.model_name IS NOT NULL
    AND btrim(sl.brand_name) <> ''
    AND btrim(sl.model_name) <> ''
    AND (
      sl.trim_name IS NULL
      OR btrim(sl.trim_name) = ''
      OR lower(btrim(sl.trim_name)) IN ('پایه', 'ساده', 'base', 'basic', 'std', 'standard', 'نامشخص')
    )
),
target_rows AS (
  SELECT ln.id, bt.id AS target_ref_id
  FROM listing_norm ln
  LEFT JOIN brand_alias ba
    ON ba.alias_brand_norm = ln.brand_norm
  LEFT JOIN model_alias ma
    ON ma.alias_key = concat(COALESCE(ba.canonical_brand_norm, ln.brand_norm), '|', ln.model_norm)
  INNER JOIN base_trim bt
    ON bt.rn = 1
   AND bt.brand_norm = COALESCE(ma.canonical_brand_norm, ba.canonical_brand_norm, ln.brand_norm)
   AND bt.model_norm = COALESCE(ma.canonical_model_norm, ln.model_norm)
)
UPDATE scraped_listings sl
SET car_reference_id = tr.target_ref_id
FROM target_rows tr
WHERE sl.id = tr.id
"""
)

_PLAN_COUNT_SQL = text(
    """
WITH base_trim AS (
  SELECT
    lower(
      btrim(
        regexp_replace(
          translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS brand_norm,
    lower(
      btrim(
        regexp_replace(
          translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS model_norm,
    ROW_NUMBER() OVER (
      PARTITION BY
        lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))),
        lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g')))
      ORDER BY
        CASE
          WHEN lower(btrim(ce.trim)) IN ('پایه', 'ساده', 'base', 'basic', 'std', 'standard') THEN 0
          WHEN ce.trim LIKE '%پایه%' OR ce.trim LIKE '%ساده%' THEN 1
          ELSE 2
        END,
        char_length(btrim(ce.trim)) ASC,
        ce.id ASC
    ) AS rn
  FROM car_entities ce
),
brand_alias AS (
  SELECT
    lower(btrim(mpd.source_string)) AS alias_brand_norm,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_brand_norm
  FROM mapping_provider_data mpd
  INNER JOIN car_entities ce ON ce.id = mpd.target_reference_id
  WHERE mpd.provider_name = 'divar_brand_alias'
    AND mpd.target_reference_id IS NOT NULL
),
model_alias AS (
  SELECT
    lower(btrim(mpd.source_string)) AS alias_key,
    lower(btrim(regexp_replace(translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_brand_norm,
    lower(btrim(regexp_replace(translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'), '\\s+', ' ', 'g'))) AS canonical_model_norm
  FROM mapping_provider_data mpd
  INNER JOIN car_entities ce ON ce.id = mpd.target_reference_id
  WHERE mpd.provider_name = 'divar_model_alias'
    AND mpd.target_reference_id IS NOT NULL
),
listing_norm AS (
  SELECT
    sl.id,
    lower(
      btrim(
        regexp_replace(
          translate(sl.brand_name, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS brand_norm,
    lower(
      btrim(
        regexp_replace(
          translate(sl.model_name, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'),
          '\\s+',
          ' ',
          'g'
        )
      )
    ) AS model_norm
  FROM scraped_listings sl
  WHERE sl.source = 'divar'
    AND sl.car_reference_id IS NULL
    AND sl.brand_name IS NOT NULL
    AND sl.model_name IS NOT NULL
    AND btrim(sl.brand_name) <> ''
    AND btrim(sl.model_name) <> ''
    AND (
      sl.trim_name IS NULL
      OR btrim(sl.trim_name) = ''
      OR lower(btrim(sl.trim_name)) IN ('پایه', 'ساده', 'base', 'basic', 'std', 'standard', 'نامشخص')
    )
)
SELECT COUNT(*)::bigint AS candidate_rows
FROM listing_norm ln
LEFT JOIN brand_alias ba
  ON ba.alias_brand_norm = ln.brand_norm
LEFT JOIN model_alias ma
  ON ma.alias_key = concat(COALESCE(ba.canonical_brand_norm, ln.brand_norm), '|', ln.model_norm)
INNER JOIN base_trim bt
  ON bt.rn = 1
 AND bt.brand_norm = COALESCE(ma.canonical_brand_norm, ba.canonical_brand_norm, ln.brand_norm)
 AND bt.model_norm = COALESCE(ma.canonical_model_norm, ln.model_norm)
"""
)

_COVERAGE_1402_SQL = text(
    """
SELECT
  COUNT(*)::bigint AS total_1402,
  COUNT(*) FILTER (WHERE car_reference_id IS NOT NULL)::bigint AS mapped_1402
FROM listings_clean
WHERE model_year = 1402
"""
)


def coverage_1402(session) -> tuple[int, int, float]:
    row = session.execute(_COVERAGE_1402_SQL).mappings().one()
    total = int(row["total_1402"] or 0)
    mapped = int(row["mapped_1402"] or 0)
    pct = round((mapped / total * 100.0), 2) if total else 0.0
    return total, mapped, pct


def _to_sql_values(rows: tuple[tuple[str, ...], ...]) -> str:
    def q(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return ", ".join("(" + ", ".join(q(col) for col in row) + ")" for row in rows)


def seed_alias_rows(session) -> None:
    brand_sql = text(_SEED_BRAND_ALIASES_SQL.text.format(brand_seed_values=_to_sql_values(_BRAND_ALIAS_SEEDS)))
    model_sql = text(_SEED_MODEL_ALIASES_SQL.text.format(model_seed_values=_to_sql_values(_MODEL_ALIAS_SEEDS)))
    session.execute(brand_sql)
    session.execute(model_sql)
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show impact only, no updates.")
    parser.add_argument(
        "--sync-listings-clean",
        action="store_true",
        help="Sync listings_clean after updating scraped_listings.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    with SessionLocal() as session:
        seed_alias_rows(session)
        total_before, mapped_before, pct_before = coverage_1402(session)
        planned = int(session.execute(_PLAN_COUNT_SQL).scalar_one())
        LOGGER.info("1402 mapped coverage before: %s/%s (%.2f%%)", mapped_before, total_before, pct_before)
        LOGGER.info("Divar orphan rows eligible for base-trim relink: %s", planned)

        if args.dry_run:
            return

        result = session.execute(_UPDATE_SQL)
        session.commit()
        updated = int(result.rowcount or 0)
        LOGGER.info("Updated Divar orphan rows: %s", updated)

    if args.sync_listings_clean:
        synced = sync_listings_clean_from_scraped()
        LOGGER.info("listings_clean sync rowcount: %s", synced)
        with SessionLocal() as session2:
            total_after, mapped_after, pct_after = coverage_1402(session2)
            LOGGER.info("1402 mapped coverage after: %s/%s (%.2f%%)", mapped_after, total_after, pct_after)
            LOGGER.info("Coverage delta: %.2fpp", pct_after - pct_before)


if __name__ == "__main__":
    main()
