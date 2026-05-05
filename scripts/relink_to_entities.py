#!/usr/bin/env python3
"""
Normalize car_entities production years (Jalali offset fix), reset scraped listing links,
then relink listings to car_entities:

- Bama: direct match on brand_name, model_name, trim_name ↔ car_entities (same taxonomy).
- Divar: mapping_provider_data + fuzzy gap-fill (relink_from_mapping helpers); never inserts car_entities.

Orphans (no trim / no mapping match) are counted and sampled in logs only.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal  # noqa: E402
from app.listings_clean_sync import sync_listings_clean_from_scraped  # noqa: E402

LOGGER = logging.getLogger("relink-to-entities")

YEAR_OFFSET_THRESHOLD = 2500
YEAR_OFFSET_DELTA = 1300


def _load_relink_from_mapping():
    path = Path(__file__).resolve().parent / "relink_from_mapping.py"
    spec = importlib.util.spec_from_file_location("relink_from_mapping", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load relink_from_mapping from {path}")
    mod = importlib.util.module_from_spec(spec)
    # Required so dataclasses / typing resolve cls.__module__ during exec_module.
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def normalize_production_years_sql(session, *, dry_run: bool) -> tuple[int, int]:
    """Subtract YEAR_OFFSET_DELTA from production_year_* when values are >= YEAR_OFFSET_THRESHOLD."""
    count_start = session.execute(
        text(
            """
            SELECT COUNT(*) FROM car_entities
            WHERE production_year_start IS NOT NULL AND production_year_start >= :threshold
            """
        ),
        {"threshold": YEAR_OFFSET_THRESHOLD},
    ).scalar_one()
    count_end = session.execute(
        text(
            """
            SELECT COUNT(*) FROM car_entities
            WHERE production_year_end IS NOT NULL AND production_year_end >= :threshold
            """
        ),
        {"threshold": YEAR_OFFSET_THRESHOLD},
    ).scalar_one()
    if dry_run:
        LOGGER.info(
            "[dry-run] Would normalize Jalali-offset years: production_year_start rows=%s, end rows=%s",
            int(count_start),
            int(count_end),
        )
        return int(count_start), int(count_end)

    session.execute(
        text(
            """
            UPDATE car_entities
            SET production_year_start = production_year_start - :delta
            WHERE production_year_start IS NOT NULL
              AND production_year_start >= :threshold
            """
        ),
        {"delta": YEAR_OFFSET_DELTA, "threshold": YEAR_OFFSET_THRESHOLD},
    )
    session.execute(
        text(
            """
            UPDATE car_entities
            SET production_year_end = production_year_end - :delta
            WHERE production_year_end IS NOT NULL
              AND production_year_end >= :threshold
            """
        ),
        {"delta": YEAR_OFFSET_DELTA, "threshold": YEAR_OFFSET_THRESHOLD},
    )
    session.commit()
    LOGGER.info(
        "Normalized production years (-%s where value >= %s): start affected≈%s, end affected≈%s",
        YEAR_OFFSET_DELTA,
        YEAR_OFFSET_THRESHOLD,
        int(count_start),
        int(count_end),
    )
    return int(count_start), int(count_end)


def reset_scraped_listing_links(session, *, dry_run: bool) -> int:
    pending = session.execute(
        text("SELECT COUNT(*) FROM scraped_listings WHERE car_reference_id IS NOT NULL")
    ).scalar_one()
    n = int(pending)
    if dry_run:
        LOGGER.info("[dry-run] Would set car_reference_id to NULL on %s scraped_listings rows", n)
        return n
    session.execute(text("UPDATE scraped_listings SET car_reference_id = NULL WHERE car_reference_id IS NOT NULL"))
    session.commit()
    LOGGER.info("Reset car_reference_id on %s scraped_listings rows", n)
    return n


_BAMA_MATCH_SQL = text(
    """
    UPDATE scraped_listings sl
    SET car_reference_id = ce.id
    FROM car_entities ce
    WHERE sl.source = 'bama'
      AND sl.brand_name IS NOT NULL AND btrim(sl.brand_name) <> ''
      AND sl.model_name IS NOT NULL AND btrim(sl.model_name) <> ''
      AND sl.trim_name IS NOT NULL AND btrim(sl.trim_name) <> ''
      AND lower(
            btrim(
              regexp_replace(
                translate(sl.brand_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
       = lower(
            btrim(
              regexp_replace(
                translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
      AND lower(
            btrim(
              regexp_replace(
                translate(sl.model_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
       = lower(
            btrim(
              regexp_replace(
                translate(ce.model, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
      AND lower(
            btrim(
              regexp_replace(
                translate(sl.trim_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
       = lower(
            btrim(
              regexp_replace(
                translate(ce.trim, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                '\\s+',
                ' ',
                'g'
              )
            )
          )
    """
)


def count_bama_relink_candidates(session) -> int:
    q = text(
        """
        SELECT COUNT(*) FROM scraped_listings sl
        INNER JOIN car_entities ce ON (
          sl.source = 'bama'
          AND sl.brand_name IS NOT NULL AND btrim(sl.brand_name) <> ''
          AND sl.model_name IS NOT NULL AND btrim(sl.model_name) <> ''
          AND sl.trim_name IS NOT NULL AND btrim(sl.trim_name) <> ''
          AND lower(
                btrim(
                  regexp_replace(
                    translate(sl.brand_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
           = lower(
                btrim(
                  regexp_replace(
                    translate(ce.brand, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
          AND lower(
                btrim(
                  regexp_replace(
                    translate(sl.model_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
           = lower(
                btrim(
                  regexp_replace(
                    translate(ce.model, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
          AND lower(
                btrim(
                  regexp_replace(
                    translate(sl.trim_name, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
           = lower(
                btrim(
                  regexp_replace(
                    translate(ce.trim, '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              )
        )
        """
    )
    return int(session.execute(q).scalar_one())


def relink_bama(session, *, dry_run: bool) -> int:
    planned = count_bama_relink_candidates(session)
    if dry_run:
        LOGGER.info("[dry-run] Bama listings matching car_entities (brand+model+trim): %s rows would update", planned)
        return planned
    result = session.execute(_BAMA_MATCH_SQL)
    session.commit()
    n = int(result.rowcount or 0)
    LOGGER.info("Bama relink updated rows: %s", n)
    return n


def log_bama_orphans(session, *, sample: int) -> int:
    """Bama rows still unlinked: missing trim or no entity match."""
    orphan_q = text(
        """
        SELECT COUNT(*) FROM scraped_listings
        WHERE source = 'bama' AND car_reference_id IS NULL
        """
    )
    orphans = int(session.execute(orphan_q).scalar_one())
    if orphans == 0:
        LOGGER.info("Bama orphans after relink: 0")
        return 0
    LOGGER.warning("Bama orphans (car_reference_id IS NULL): %s", orphans)
    sample_q = text(
        """
        SELECT id, brand_name, model_name, trim_name
        FROM scraped_listings
        WHERE source = 'bama' AND car_reference_id IS NULL
        ORDER BY id
        LIMIT :lim
        """
    )
    rows = session.execute(sample_q, {"lim": sample}).mappings().all()
    for row in rows:
        LOGGER.warning(
            "[bama-orphan] id=%s brand=%r model=%r trim=%r",
            row["id"],
            row["brand_name"],
            row["model_name"],
            row["trim_name"],
        )
    return orphans


def log_divar_orphans(rfm, session, *, sample: int) -> int:
    orphans_q = text(
        """
        SELECT COUNT(*) FROM scraped_listings
        WHERE source = 'divar' AND car_reference_id IS NULL
        """
    )
    orphans = int(session.execute(orphans_q).scalar_one())
    if orphans == 0:
        LOGGER.info("Divar orphans after relink: 0")
        return 0
    LOGGER.warning("Divar orphans (car_reference_id IS NULL): %s", orphans)
    top = rfm.top_unlinked_source_names(provider_name="divar", top_n=sample)
    for row in top:
        LOGGER.warning("[divar-orphan-cluster] source_norm=%r count=%s", row["source_norm"], row["cnt"])
    return orphans


def coverage_snapshot(session) -> dict[str, int | float]:
    row = session.execute(
        text(
            """
            SELECT
              COUNT(*)::bigint AS total,
              COUNT(*) FILTER (WHERE car_reference_id IS NOT NULL)::bigint AS linked,
              COUNT(*) FILTER (WHERE source = 'bama')::bigint AS bama_total,
              COUNT(*) FILTER (WHERE source = 'bama' AND car_reference_id IS NOT NULL)::bigint AS bama_linked,
              COUNT(*) FILTER (WHERE source = 'divar')::bigint AS divar_total,
              COUNT(*) FILTER (WHERE source = 'divar' AND car_reference_id IS NOT NULL)::bigint AS divar_linked
            FROM scraped_listings
            """
        )
    ).mappings().one()
    total = int(row["total"])
    linked = int(row["linked"])
    pct = round((linked / total * 100.0), 2) if total else 0.0
    return {
        "total": total,
        "linked": linked,
        "coverage_pct": pct,
        "bama_total": int(row["bama_total"]),
        "bama_linked": int(row["bama_linked"]),
        "divar_total": int(row["divar_total"]),
        "divar_linked": int(row["divar_linked"]),
    }


def run_divar_pipeline(
    rfm,
    *,
    dry_run: bool,
    fill_gap_top_n: int,
    min_score: int,
    review_score_min: int,
    review_score_max: int,
    discover_mapping_gaps_top_n: int,
) -> int:
    """
    Fuzzy-fill mapping_provider_data gaps and apply mappings to listings.
    Does not call seed_high_impact_references / ensure_car_reference (no new car_entities rows).
    """
    rfm.ensure_mapping_review_columns()
    references = rfm.load_all_car_references()
    if not references:
        raise RuntimeError("car_entities is empty — load calculator taxonomy before relinking.")

    gaps = rfm.top_unlinked_mapping_gaps(provider_name="divar", top_n=fill_gap_top_n)
    LOGGER.info("Divar: examining up to %s mapping gaps (unlinked listings × NULL target rows)", fill_gap_top_n)
    rfm.update_mapping_targets(gaps=gaps, references=references, min_score=min_score, dry_run=dry_run)

    rfm.backfill_missing_mapping_scores(
        provider_name="divar",
        references=references,
        review_score_min=review_score_min,
        review_score_max=review_score_max,
        dry_run=dry_run,
    )

    relinked = rfm.relink_from_mapping_table(provider_name="divar", dry_run=dry_run)

    if not dry_run and discover_mapping_gaps_top_n > 0:
        inserted = rfm.collect_and_store_needs_mapping(
            provider_name="divar",
            top_n=discover_mapping_gaps_top_n,
            dry_run=False,
        )
        LOGGER.info("Divar: inserted placeholder mapping rows for discovery: %s", inserted)

    return int(relinked)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log planned changes only (no commits).")
    parser.add_argument("--skip-normalize-years", action="store_true")
    parser.add_argument("--skip-reset-links", action="store_true")
    parser.add_argument("--skip-bama", action="store_true")
    parser.add_argument("--skip-divar", action="store_true")
    parser.add_argument("--divar-fill-gap-top-n", type=int, default=50)
    parser.add_argument("--divar-min-score", type=int, default=72)
    parser.add_argument("--divar-review-score-min", type=int, default=58)
    parser.add_argument("--divar-review-score-max", type=int, default=68)
    parser.add_argument("--divar-discover-gaps-top-n", type=int, default=30, help="Insert unmapped Divar clusters into mapping_provider_data (target NULL).")
    parser.add_argument("--orphan-sample", type=int, default=20)
    parser.add_argument(
        "--sync-listings-clean",
        action="store_true",
        help="After successful run, refresh listings_clean from scraped_listings.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(args.verbose)

    rfm = _load_relink_from_mapping()

    with SessionLocal() as session:
        if not args.skip_normalize_years:
            normalize_production_years_sql(session, dry_run=args.dry_run)
        elif args.dry_run:
            LOGGER.info("Skipping year normalization (--skip-normalize-years).")

        if not args.skip_reset_links:
            reset_scraped_listing_links(session, dry_run=args.dry_run)
        elif args.dry_run:
            LOGGER.info("Skipping link reset (--skip-reset-links).")

        if not args.skip_bama:
            relink_bama(session, dry_run=args.dry_run)
            if not args.dry_run:
                log_bama_orphans(session, sample=args.orphan_sample)
        else:
            LOGGER.info("Skipping Bama relink (--skip-bama).")

        if not args.skip_divar:
            # Divar helpers open their own SessionLocal transactions internally.
            run_divar_pipeline(
                rfm,
                dry_run=args.dry_run,
                fill_gap_top_n=args.divar_fill_gap_top_n,
                min_score=args.divar_min_score,
                review_score_min=args.divar_review_score_min,
                review_score_max=args.divar_review_score_max,
                discover_mapping_gaps_top_n=args.divar_discover_gaps_top_n,
            )
            if not args.dry_run:
                with SessionLocal() as session2:
                    log_divar_orphans(rfm, session2, sample=args.orphan_sample)
        else:
            LOGGER.info("Skipping Divar relink (--skip-divar).")

        stats = coverage_snapshot(session)
        LOGGER.info(
            "Coverage (all scraped_listings): %s / %s mapped (%.2f%%)",
            stats["linked"],
            stats["total"],
            stats["coverage_pct"],
        )
        LOGGER.info(
            "  Bama: %s / %s | Divar: %s / %s",
            stats["bama_linked"],
            stats["bama_total"],
            stats["divar_linked"],
            stats["divar_total"],
        )

    if args.sync_listings_clean and not args.dry_run:
        n = sync_listings_clean_from_scraped()
        LOGGER.info("listings_clean sync rowcount: %s", n)


if __name__ == "__main__":
    main()
