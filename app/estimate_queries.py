"""Price estimation queries against listings_clean (PRD: percentiles P10–P90, car_reference_id + model_year filters)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Confidence by sample size (listings_clean rows in the cohort used for the estimate).
# Tiers ordered from largest minimum count to smallest; first match wins.
# Adjust to match formal PRD table if provided.
CONFIDENCE_TIERS: tuple[tuple[int, str, str], ...] = (
    (150, "very_high", "بسیار بالا"),
    (60, "high", "بالا"),
    (25, "medium", "متوسط"),
    (8, "low", "پایین"),
    (1, "very_low", "بسیار پایین"),
)


def confidence_from_sample_size(n: int) -> tuple[str, str]:
    if n <= 0:
        return "insufficient", "ناکافی"
    for min_n, code, label_fa in CONFIDENCE_TIERS:
        if n >= min_n:
            return code, label_fa
    return "very_low", "بسیار پایین"


_BASE_WHERE = """
FROM listings_clean lc
INNER JOIN car_entities cr ON cr.id = lc.car_reference_id
WHERE lc.car_reference_id IS NOT NULL
  AND lc.price IS NOT NULL
  AND lc.price > 0
  AND lower(btrim(cr.brand)) = :brand_exact
  AND lower(
        btrim(
            translate(cr.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
        )
      ) = :model_exact
"""

_STATS_SELECT = """
SELECT
  COUNT(*)::bigint AS sample_size,
  percentile_cont(ARRAY[0.10, 0.25, 0.50, 0.75, 0.90]) WITHIN GROUP (ORDER BY lc.price::double precision) AS pct
"""


def _parse_percentile_array(row: dict[str, Any] | None) -> tuple[int, list[float] | None]:
    if not row:
        return 0, None
    n = int(row["sample_size"] or 0)
    raw = row.get("pct")
    if n == 0 or raw is None:
        return n, None
    if hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes)):
        vals = list(raw)
    else:
        vals = [raw]
    floats = [float(x) for x in vals if x is not None]
    if len(floats) < 5:
        return n, None
    return n, floats


def fetch_exact_cohort_stats(
    db: Session,
    *,
    brand_exact: str,
    model_exact: str,
    year_min: int,
    year_max: int,
    mileage_min: int,
    mileage_max: int,
) -> tuple[int, list[float] | None]:
    sql = text(
        _STATS_SELECT
        + _BASE_WHERE
        + """
          AND lc.model_year IS NOT NULL
          AND lc.model_year BETWEEN :year_min AND :year_max
          AND lc.mileage IS NOT NULL
          AND lc.mileage BETWEEN :mileage_min AND :mileage_max
        """
    )
    row = db.execute(
        sql,
        {
            "brand_exact": brand_exact,
            "model_exact": model_exact,
            "year_min": year_min,
            "year_max": year_max,
            "mileage_min": mileage_min,
            "mileage_max": mileage_max,
        },
    ).mappings().first()
    return _parse_percentile_array(dict(row) if row else {})


def fetch_exact_cohort_stats_by_reference_id(
    db: Session,
    *,
    car_reference_id: int,
    year_min: int,
    year_max: int,
    mileage_min: int,
    mileage_max: int,
) -> tuple[int, list[float] | None]:
    sql = text(
        _STATS_SELECT
        + """
FROM listings_clean lc
WHERE lc.car_reference_id = :car_reference_id
  AND lc.price IS NOT NULL
  AND lc.price > 0
  AND lc.model_year IS NOT NULL
  AND lc.model_year BETWEEN :year_min AND :year_max
  AND lc.mileage IS NOT NULL
  AND lc.mileage BETWEEN :mileage_min AND :mileage_max
"""
    )
    row = db.execute(
        sql,
        {
            "car_reference_id": car_reference_id,
            "year_min": year_min,
            "year_max": year_max,
            "mileage_min": mileage_min,
            "mileage_max": mileage_max,
        },
    ).mappings().first()
    return _parse_percentile_array(dict(row) if row else {})


def fetch_fallback_stats_by_reference_id(
    db: Session,
    *,
    car_reference_id: int,
) -> tuple[int, list[float] | None, float]:
    sql = text(
        """
SELECT
  COUNT(*)::bigint AS sample_size,
  percentile_cont(ARRAY[0.10, 0.25, 0.50, 0.75, 0.90]) WITHIN GROUP (ORDER BY lc.price::double precision) AS pct,
  AVG(COALESCE(lc.mileage, 0)::double precision) AS avg_mileage
FROM listings_clean lc
WHERE lc.car_reference_id = :car_reference_id
  AND lc.price IS NOT NULL
  AND lc.price > 0
"""
    )
    row = db.execute(sql, {"car_reference_id": car_reference_id}).mappings().first()
    if not row:
        return 0, None, 0.0
    n, pct = _parse_percentile_array(dict(row))
    avg_m = float(row["avg_mileage"] or 0.0)
    return n, pct, avg_m


def fetch_sibling_fallback_stats_by_reference_id(
    db: Session,
    *,
    car_reference_id: int,
    primary_weight: float = 2.0,
) -> tuple[int, list[float] | None, float]:
    sql = text(
        """
WITH base_ref AS (
  SELECT
    lower(btrim(brand)) AS brand_exact,
    lower(
      btrim(
        translate(model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
      )
    ) AS model_exact
  FROM car_entities
  WHERE id = :car_reference_id
),
per_ref AS (
  SELECT
    lc.car_reference_id AS ref_id,
    COUNT(*)::bigint AS sample_size,
    percentile_cont(ARRAY[0.10, 0.25, 0.50, 0.75, 0.90]) WITHIN GROUP (ORDER BY lc.price::double precision) AS pct,
    AVG(COALESCE(lc.mileage, 0)::double precision) AS avg_mileage
  FROM listings_clean lc
  INNER JOIN car_entities ce ON ce.id = lc.car_reference_id
  INNER JOIN base_ref br ON
      lower(btrim(ce.brand)) = br.brand_exact
      AND lower(
            btrim(
              translate(ce.model, '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
            )
          ) = br.model_exact
  WHERE lc.price IS NOT NULL
    AND lc.price > 0
  GROUP BY lc.car_reference_id
)
SELECT ref_id, sample_size, pct, avg_mileage
FROM per_ref
WHERE sample_size > 0
"""
    )
    rows = db.execute(sql, {"car_reference_id": car_reference_id}).mappings().all()
    if not rows:
        return 0, None, 0.0

    total_rows = 0
    total_weight = 0.0
    weighted_avg_mileage = 0.0
    weighted_percentiles = [0.0, 0.0, 0.0, 0.0, 0.0]

    for row in rows:
        n = int(row["sample_size"] or 0)
        if n <= 0:
            continue
        raw_pct = row.get("pct")
        if not raw_pct or len(raw_pct) < 5:
            continue
        pct = [float(x) for x in raw_pct]
        weight_multiplier = primary_weight if int(row["ref_id"]) == car_reference_id else 1.0
        weighted_n = float(n) * float(weight_multiplier)

        total_rows += n
        total_weight += weighted_n
        weighted_avg_mileage += float(row["avg_mileage"] or 0.0) * weighted_n
        for idx in range(5):
            weighted_percentiles[idx] += pct[idx] * weighted_n

    if total_rows <= 0 or total_weight <= 0:
        return 0, None, 0.0

    merged_pct = [p / total_weight for p in weighted_percentiles]
    merged_avg_mileage = weighted_avg_mileage / total_weight
    return total_rows, merged_pct, merged_avg_mileage


def fetch_model_fallback_stats(
    db: Session,
    *,
    brand_exact: str,
    model_exact: str,
) -> tuple[int, list[float] | None, float]:
    sql = text(
        """
SELECT
  COUNT(*)::bigint AS sample_size,
  percentile_cont(ARRAY[0.10, 0.25, 0.50, 0.75, 0.90]) WITHIN GROUP (ORDER BY lc.price::double precision) AS pct,
  AVG(COALESCE(lc.mileage, 0)::double precision) AS avg_mileage
"""
        + _BASE_WHERE
    )
    row = db.execute(
        sql,
        {
            "brand_exact": brand_exact,
            "model_exact": model_exact,
        },
    ).mappings().first()
    if not row:
        return 0, None, 0.0
    n, pct = _parse_percentile_array(dict(row))
    avg_m = float(row["avg_mileage"] or 0.0)
    return n, pct, avg_m


def scale_percentiles(pct: list[float] | None, factor: Decimal) -> list[float] | None:
    if not pct:
        return None
    f = float(factor)
    return [p * f for p in pct]


def percentiles_to_ints(pct: list[float] | None) -> dict[str, int | None]:
    if not pct or len(pct) < 5:
        return {"p10": None, "p25": None, "median": None, "p75": None, "p90": None}
    return {
        "p10": int(round(pct[0])),
        "p25": int(round(pct[1])),
        "median": int(round(pct[2])),
        "p75": int(round(pct[3])),
        "p90": int(round(pct[4])),
    }


SQL_AVAILABLE_CARS_FROM_CLEAN = text(
    """
    SELECT lc.brand_name, lc.model_name
    FROM listings_clean lc
    WHERE lc.car_reference_id IS NOT NULL
      AND lc.price IS NOT NULL
      AND lc.price > 0
      AND lc.brand_name IS NOT NULL
      AND lc.model_name IS NOT NULL
      AND btrim(lc.brand_name) <> ''
      AND btrim(lc.model_name) <> ''
    GROUP BY lc.brand_name, lc.model_name
    ORDER BY lc.brand_name, lc.model_name
    """
)
