from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


LOGGER = logging.getLogger(__name__)

SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
TERM_SYNONYMS: dict[str, str] = {
    "peugeot": "پژو",
    "jac": "جک",
    "dena": "دنا",
    "quick": "کوییک",
    "saipa": "سایپا",
    "ikco": "ایران خودرو",
    "tara": "تارا",
    "shahin": "شاهین",
    "rana": "رانا",
    "v 1": "v1",
    "v 4": "v4",
    "h 30": "h30",
    "j 4": "j4",
    "s x": "sx",
    "e x": "ex",
    "l x": "lx",
}
BRAND_ALIASES: dict[str, str] = {
    "پژو": "پژو",
    "peugeot": "پژو",
    "تیبا": "تیبا",
    "تارا": "تارا",
    "tara": "تارا",
    "پیکان": "پیکان",
    "شاهین": "شاهین",
    "shahin": "شاهین",
    "کوییک": "کوییک",
    "quick": "کوییک",
    "دنا": "دنا",
    "dena": "دنا",
    "لاماری": "لاماری",
    "jac": "جک",
    "جک": "جک",
    "رنو": "رنو",
    "renault": "رنو",
    "رانا": "رانا",
    "سیتروئن": "سیتروئن",
    "citroen": "سیتروئن",
    "اطلس": "اطلس",
    "نیسان": "نیسان",
    "ایران خودرو": "ایران خودرو",
    "ری را": "ایران خودرو",
}


@dataclass
class CarReferenceRow:
    id: int
    brand: str
    model: str
    trim: str
    full_norm: str


@dataclass
class ReferenceSeed:
    source_string: str
    brand: str
    model: str
    trim: str


MANUAL_REFERENCE_SEEDS: list[ReferenceSeed] = [
    ReferenceSeed(source_string="سایپا اطلس g", brand="اطلس", model="دنده ای تیپ G", trim="Base"),
    ReferenceSeed(source_string="kmc x5", brand="کی ام سی", model="X5", trim="Base"),
    ReferenceSeed(source_string="کی ام سی ایکس 5", brand="کی ام سی", model="X5", trim="Base"),
]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS).replace("\u200c", " ").strip().lower()
    cleaned = NON_WORD_RE.sub(" ", cleaned)
    cleaned = SPACE_RE.sub(" ", cleaned).strip()
    for src, dst in sorted(TERM_SYNONYMS.items(), key=lambda item: len(item[0]), reverse=True):
        cleaned = re.sub(rf"\b{re.escape(src)}\b", dst, cleaned)
    return SPACE_RE.sub(" ", cleaned).strip()


def similarity_score(a: str, b: str) -> int:
    if not a or not b:
        return 0
    return int(SequenceMatcher(None, a, b).ratio() * 100)


def ensure_mapping_review_columns() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS mapping_provider_data (
            id BIGSERIAL PRIMARY KEY,
            provider_name VARCHAR(50) NOT NULL,
            source_string VARCHAR(300) NOT NULL,
            target_reference_id INTEGER NULL REFERENCES car_entities(id) ON DELETE SET NULL,
            match_score INTEGER NULL,
            is_verified BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider_name, source_string)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_mapping_provider_data_provider_target
        ON mapping_provider_data(provider_name, target_reference_id)
        """,
        """
        ALTER TABLE mapping_provider_data
        ADD COLUMN IF NOT EXISTS match_score INTEGER NULL
        """,
        """
        ALTER TABLE mapping_provider_data
        ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT TRUE
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_mapping_provider_data_is_verified
        ON mapping_provider_data(is_verified)
        """,
    ]
    with SessionLocal() as session:
        for stmt in statements:
            session.execute(text(stmt))
        session.commit()


def load_all_car_references() -> list[CarReferenceRow]:
    query = text(
        """
        SELECT id, brand, model, trim
        FROM car_entities
        ORDER BY id
        """
    )
    with SessionLocal() as session:
        rows = session.execute(query).mappings().all()
    return [
        CarReferenceRow(
            id=int(row["id"]),
            brand=str(row["brand"] or ""),
            model=str(row["model"] or ""),
            trim=str(row["trim"] or ""),
            full_norm=normalize_text(
                f"{row['brand'] or ''} {row['model'] or ''} {row['trim'] or ''}"
            ),
        )
        for row in rows
    ]


def top_unlinked_mapping_gaps(provider_name: str, top_n: int) -> list[dict]:
    query = text(
        """
        SELECT
            mpd.id,
            mpd.source_string,
            COUNT(sl.id) AS unlinked_count
        FROM mapping_provider_data mpd
        LEFT JOIN scraped_listings sl
          ON sl.car_reference_id IS NULL
         AND lower(sl.source) LIKE :provider_prefix
         AND lower(
               btrim(
                   regexp_replace(
                       translate(concat_ws(' ', COALESCE(sl.brand_name, ''), COALESCE(sl.model_name, '')), '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                       '\\s+',
                       ' ',
                       'g'
                   )
               )
             ) = lower(btrim(mpd.source_string))
        WHERE mpd.provider_name = :provider_name
          AND mpd.target_reference_id IS NULL
        GROUP BY mpd.id, mpd.source_string
        ORDER BY unlinked_count DESC, mpd.id ASC
        LIMIT :top_n
        """
    )
    with SessionLocal() as session:
        return session.execute(
            query,
            {"provider_name": provider_name, "provider_prefix": f"{provider_name}%", "top_n": top_n},
        ).mappings().all()


def pick_best_reference(source_string: str, references: list[CarReferenceRow], min_score: int) -> tuple[int | None, int]:
    source_norm = normalize_text(source_string)
    if not source_norm:
        return None, 0
    inferred_brand = infer_brand(source_norm)
    if inferred_brand and not has_non_brand_tokens(source_norm, inferred_brand):
        return None, 0
    candidate_refs = references
    if inferred_brand:
        filtered = [ref for ref in references if normalize_text(ref.brand) == inferred_brand]
        if not filtered:
            # Brand inferred from source but absent in car_reference: avoid cross-brand false positives.
            return None, 0
        candidate_refs = filtered

    best_ref_id: int | None = None
    best_score = -1
    for ref in candidate_refs:
        score = similarity_score(source_norm, ref.full_norm)
        if source_norm in ref.full_norm or ref.full_norm in source_norm:
            score += 8
        if inferred_brand and normalize_text(ref.brand) == inferred_brand:
            score += 10
        if score > best_score:
            best_score = score
            best_ref_id = ref.id
    if best_score < min_score:
        return None, best_score
    return best_ref_id, best_score


def infer_brand(source_norm: str) -> str | None:
    for alias, canonical_brand in sorted(BRAND_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        alias_norm = normalize_text(alias)
        if alias_norm and alias_norm in source_norm:
            return normalize_text(canonical_brand)
    return None


def has_non_brand_tokens(source_norm: str, inferred_brand: str) -> bool:
    source_tokens = [tok for tok in source_norm.split(" ") if tok]
    brand_tokens = {tok for tok in inferred_brand.split(" ") if tok}
    extra_tokens = [tok for tok in source_tokens if tok not in brand_tokens]
    return len(extra_tokens) > 0


def update_mapping_targets(gaps: list[dict], references: list[CarReferenceRow], min_score: int, dry_run: bool) -> int:
    if not gaps:
        return 0
    update_query = text(
        """
        UPDATE mapping_provider_data
        SET target_reference_id = :target_reference_id,
            match_score = :match_score,
            is_verified = :is_verified,
            updated_at = NOW()
        WHERE id = :id
        """
    )
    updated = 0
    for gap in gaps:
        row_id = int(gap["id"])
        source_string = str(gap["source_string"] or "")
        unlinked_count = int(gap["unlinked_count"] or 0)
        target_ref_id, score = pick_best_reference(source_string, references, min_score=min_score)
        if target_ref_id is None:
            print(
                f"[mapping-skip] mapping_id={row_id} source={source_string!r} "
                f"unlinked={unlinked_count} best_score={score} (<{min_score})"
            )
            continue
        print(
            f"[mapping-match] mapping_id={row_id} source={source_string!r} "
            f"-> target_reference_id={target_ref_id} score={score} unlinked={unlinked_count}"
        )
        is_verified = score > 68
        if dry_run:
            updated += 1
            continue
        with SessionLocal() as session:
            session.execute(
                update_query,
                {
                    "id": row_id,
                    "target_reference_id": target_ref_id,
                    "match_score": score,
                    "is_verified": is_verified,
                },
            )
            session.commit()
            updated += 1
    return updated


def ensure_car_reference(brand: str, model: str, trim: str) -> int:
    select_query = text(
        """
        SELECT id
        FROM car_entities
        WHERE lower(btrim(brand)) = :brand_norm
          AND lower(btrim(translate(model, '۰۱۲۳۴۵۶۷۸۹', '0123456789'))) = :model_norm
          AND lower(btrim(translate(trim, '۰۱۲۳۴۵۶۷۸۹', '0123456789'))) = :trim_norm
        ORDER BY id
        LIMIT 1
        """
    )
    insert_query = text(
        """
        INSERT INTO car_entities (brand, model, trim, source_url, production_year_start, production_year_end)
        VALUES (:brand, :model, :trim, :source_url, NULL, NULL)
        ON CONFLICT (brand, model, trim)
        DO UPDATE SET source_url = EXCLUDED.source_url
        RETURNING id
        """
    )
    with SessionLocal() as session:
        existing = session.execute(
            select_query,
            {
                "brand_norm": normalize_text(brand),
                "model_norm": normalize_text(model),
                "trim_norm": normalize_text(trim),
            },
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        created = session.execute(
            insert_query,
            {
                "brand": brand,
                "model": model,
                "trim": trim,
                "source_url": "mapping-manual-seed",
            },
        ).scalar_one()
        session.commit()
    return int(created)


def seed_high_impact_references(provider_name: str, dry_run: bool) -> int:
    upsert_query = text(
        """
        INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id, match_score, is_verified)
        VALUES (:provider_name, :source_string, :target_reference_id, :match_score, :is_verified)
        ON CONFLICT (provider_name, source_string)
        DO UPDATE SET
            target_reference_id = EXCLUDED.target_reference_id,
            match_score = EXCLUDED.match_score,
            is_verified = EXCLUDED.is_verified,
            updated_at = NOW()
        """
    )
    affected = 0
    for seed in MANUAL_REFERENCE_SEEDS:
        target_id = ensure_car_reference(seed.brand, seed.model, seed.trim)
        print(
            f"[manual-seed] source={seed.source_string!r} "
            f"-> target={seed.brand}/{seed.model}/{seed.trim} target_reference_id={target_id}"
        )
        if dry_run:
            affected += 1
            continue
        with SessionLocal() as session:
            session.execute(
                upsert_query,
                {
                    "provider_name": provider_name,
                    "source_string": normalize_text(seed.source_string),
                    "target_reference_id": target_id,
                    "match_score": 100,
                    "is_verified": True,
                },
            )
            session.commit()
            affected += 1
    return affected


def backfill_missing_mapping_scores(
    provider_name: str,
    references: list[CarReferenceRow],
    review_score_min: int,
    review_score_max: int,
    dry_run: bool,
) -> int:
    target_ref_map = {ref.id: ref for ref in references}
    select_query = text(
        """
        SELECT id, source_string, target_reference_id
        FROM mapping_provider_data
        WHERE provider_name = :provider_name
          AND target_reference_id IS NOT NULL
          AND match_score IS NULL
        ORDER BY id
        """
    )
    update_query = text(
        """
        UPDATE mapping_provider_data
        SET match_score = :match_score,
            is_verified = :is_verified,
            updated_at = NOW()
        WHERE id = :id
        """
    )
    with SessionLocal() as session:
        rows = session.execute(select_query, {"provider_name": provider_name}).mappings().all()
    if not rows:
        return 0
    updated = 0
    for row in rows:
        mapping_id = int(row["id"])
        target_id = int(row["target_reference_id"])
        source_norm = normalize_text(str(row["source_string"] or ""))
        ref = target_ref_map.get(target_id)
        if ref is None:
            continue
        score = similarity_score(source_norm, ref.full_norm)
        if source_norm in ref.full_norm or ref.full_norm in source_norm:
            score += 8
        is_verified = not (review_score_min <= score <= review_score_max)
        if dry_run:
            updated += 1
            continue
        with SessionLocal() as session:
            session.execute(
                update_query,
                {"id": mapping_id, "match_score": score, "is_verified": is_verified},
            )
            session.commit()
            updated += 1
    return updated


def relink_from_mapping_table(provider_name: str, dry_run: bool) -> int:
    count_query = text(
        """
        SELECT COUNT(*)
        FROM scraped_listings sl
        JOIN mapping_provider_data mpd
          ON mpd.provider_name = :provider_name
         AND mpd.target_reference_id IS NOT NULL
         AND lower(
               btrim(
                   regexp_replace(
                       translate(concat_ws(' ', COALESCE(sl.brand_name, ''), COALESCE(sl.model_name, '')), '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                       '\\s+',
                       ' ',
                       'g'
                   )
               )
             ) = lower(btrim(mpd.source_string))
        WHERE lower(sl.source) LIKE :provider_prefix
          AND (
                sl.car_reference_id IS NULL
                OR sl.car_reference_id <> mpd.target_reference_id
              )
        """
    )
    update_query = text(
        """
        UPDATE scraped_listings sl
        SET car_reference_id = mpd.target_reference_id
        FROM mapping_provider_data mpd
        WHERE mpd.provider_name = :provider_name
          AND mpd.target_reference_id IS NOT NULL
          AND lower(sl.source) LIKE :provider_prefix
          AND lower(
                btrim(
                    regexp_replace(
                        translate(concat_ws(' ', COALESCE(sl.brand_name, ''), COALESCE(sl.model_name, '')), '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                        '\\s+',
                        ' ',
                        'g'
                    )
                )
              ) = lower(btrim(mpd.source_string))
          AND (
                sl.car_reference_id IS NULL
                OR sl.car_reference_id <> mpd.target_reference_id
              )
        """
    )
    params = {"provider_name": provider_name, "provider_prefix": f"{provider_name}%"}
    with SessionLocal() as session:
        planned = int(session.execute(count_query, params).scalar_one())
    if dry_run:
        return planned
    with SessionLocal() as session:
        result = session.execute(update_query, params)
        session.commit()
        return int(result.rowcount or 0)


def provider_coverage(provider_name: str) -> tuple[int, int]:
    query = text(
        """
        SELECT
            COUNT(*) AS total_ads,
            COUNT(*) FILTER (WHERE car_reference_id IS NOT NULL) AS linked_ads
        FROM scraped_listings
        WHERE lower(source) LIKE :provider_prefix
        """
    )
    with SessionLocal() as session:
        row = session.execute(query, {"provider_prefix": f"{provider_name}%"}).mappings().one()
    return int(row["total_ads"]), int(row["linked_ads"])


def top_unlinked_source_names(provider_name: str, top_n: int) -> list[dict]:
    query = text(
        """
        SELECT
            lower(
                btrim(
                    regexp_replace(
                        translate(concat_ws(' ', COALESCE(brand_name, ''), COALESCE(model_name, '')), '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                        '\\s+',
                        ' ',
                        'g'
                    )
                )
            ) AS source_norm,
            COUNT(*) AS cnt
        FROM scraped_listings
        WHERE lower(source) LIKE :provider_prefix
          AND car_reference_id IS NULL
        GROUP BY 1
        ORDER BY cnt DESC
        LIMIT :top_n
        """
    )
    with SessionLocal() as session:
        return session.execute(
            query,
            {"provider_prefix": f"{provider_name}%", "top_n": top_n},
        ).mappings().all()


def collect_and_store_needs_mapping(provider_name: str, top_n: int, dry_run: bool) -> int:
    missing_query = text(
        """
        WITH normalized_unlinked AS (
            SELECT
                lower(
                    btrim(
                        regexp_replace(
                            translate(concat_ws(' ', COALESCE(sl.brand_name, ''), COALESCE(sl.model_name, '')), '۰۱۲۳۴۵۶۷۸۹', '0123456789'),
                            '\\s+',
                            ' ',
                            'g'
                        )
                    )
                ) AS source_norm,
                COUNT(*) AS cnt
            FROM scraped_listings sl
            WHERE sl.car_reference_id IS NULL
              AND lower(sl.source) LIKE :provider_prefix
            GROUP BY 1
        )
        SELECT source_norm, cnt
        FROM normalized_unlinked nu
        LEFT JOIN mapping_provider_data mpd
          ON mpd.provider_name = :provider_name
         AND lower(btrim(mpd.source_string)) = nu.source_norm
        WHERE mpd.id IS NULL
          AND nu.source_norm <> ''
        ORDER BY cnt DESC
        LIMIT :top_n
        """
    )
    insert_query = text(
        """
        INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id, is_verified)
        VALUES (:provider_name, :source_string, NULL, FALSE)
        ON CONFLICT (provider_name, source_string) DO NOTHING
        """
    )
    with SessionLocal() as session:
        rows = session.execute(
            missing_query,
            {"provider_name": provider_name, "provider_prefix": f"{provider_name}%", "top_n": top_n},
        ).mappings().all()
    if dry_run:
        return len(rows)
    inserted = 0
    with SessionLocal() as session:
        for row in rows:
            result = session.execute(
                insert_query,
                {"provider_name": provider_name, "source_string": str(row["source_norm"])},
            )
            inserted += int(result.rowcount or 0)
        session.commit()
    return inserted


def apply_provider_mapping_to_listings_only(
    provider: str = "divar",
    dry_run: bool = False,
) -> dict[str, object]:
    """
    Set scraped_listings.car_reference_id rows that match mapping_provider_data.
    Does not INSERT into car_reference or seed mapping rows via ensure_car_reference.
    """
    ensure_mapping_review_columns()
    relinked = relink_from_mapping_table(provider_name=provider, dry_run=dry_run)
    total_ads, linked_ads = provider_coverage(provider_name=provider)
    orphan_ads = total_ads - linked_ads
    LOGGER.info(
        "mapping_apply_only provider=%r dry_run=%s relinked_rows=%s coverage=%s/%s orphans=%s",
        provider,
        dry_run,
        relinked,
        linked_ads,
        total_ads,
        orphan_ads,
    )
    if orphan_ads > 0 and not dry_run:
        LOGGER.warning(
            "mapping_apply_only %s listings remain without car_reference_id (no new car_reference rows created here).",
            orphan_ads,
        )
    return {
        "provider": provider,
        "relinked_listings": relinked,
        "total_ads": total_ads,
        "linked_ads": linked_ads,
        "orphan_ads": orphan_ads,
        "dry_run": dry_run,
    }


def high_confidence_stats(provider_name: str) -> tuple[int, int]:
    query = text(
        """
        SELECT
            COUNT(*) FILTER (WHERE mpd.target_reference_id IS NOT NULL) AS mapped_rows,
            COUNT(*) FILTER (
                WHERE mpd.target_reference_id IS NOT NULL
                  AND COALESCE(mpd.is_verified, FALSE) = TRUE
            ) AS high_conf_rows
        FROM mapping_provider_data mpd
        WHERE mpd.provider_name = :provider_name
        """
    )
    with SessionLocal() as session:
        row = session.execute(query, {"provider_name": provider_name}).mappings().one()
    return int(row["mapped_rows"]), int(row["high_conf_rows"])


def run_relink_pipeline(
    provider: str = "divar",
    fill_gap_top_n: int = 20,
    min_score: int = 72,
    report_top_unlinked: int = 15,
    needs_mapping_top: int = 20,
    review_score_min: int = 58,
    review_score_max: int = 68,
    dry_run: bool = False,
    *,
    allow_car_reference_mutations: bool = True,
) -> dict[str, object]:
    """
    Programmatic entrypoint used by Celery mapping_task and run_normalization().

    When allow_car_reference_mutations is False, manual seeds that call ensure_car_reference() are skipped
    so workers never insert into car_reference while still filling mapping gaps / relinking listings.
    """
    print(f"Provider: {provider}")
    ensure_mapping_review_columns()
    if allow_car_reference_mutations:
        seeded = seed_high_impact_references(provider_name=provider, dry_run=dry_run)
        print(f"Manual high-impact seeds upserted: {seeded}")
    else:
        seeded = 0
        print("Skipped manual high-impact seeds (allow_car_reference_mutations=False).")

    references = load_all_car_references()
    if not references:
        raise RuntimeError("car_entities is empty. Please seed/load car_entities first.")

    gaps = top_unlinked_mapping_gaps(provider_name=provider, top_n=fill_gap_top_n)
    print(f"Top mapping gaps fetched: {len(gaps)}")
    filled = update_mapping_targets(gaps=gaps, references=references, min_score=min_score, dry_run=dry_run)
    print(f"Filled mapping_provider_data target_reference_id rows: {filled}")
    backfilled = backfill_missing_mapping_scores(
        provider_name=provider,
        references=references,
        review_score_min=review_score_min,
        review_score_max=review_score_max,
        dry_run=dry_run,
    )
    print(f"Backfilled missing mapping scores: {backfilled}")

    relinked = relink_from_mapping_table(provider_name=provider, dry_run=dry_run)
    if dry_run:
        print(f"[dry-run] listings planned for relink: {relinked}")
    else:
        print(f"Listings relinked from mapping table: {relinked}")
    needs_mapping = collect_and_store_needs_mapping(
        provider_name=provider,
        top_n=needs_mapping_top,
        dry_run=dry_run,
    )
    print(f"Needs-mapping placeholders inserted: {needs_mapping}")

    total_ads, linked_ads = provider_coverage(provider_name=provider)
    coverage = (linked_ads / total_ads * 100.0) if total_ads else 0.0
    orphan_ads = total_ads - linked_ads
    print(f"Coverage: {linked_ads}/{total_ads} ({coverage:.2f}%)")
    print(f"Unlinked ads remaining: {orphan_ads}")

    flagged_low_conf_query = text(
        """
        UPDATE mapping_provider_data
        SET is_verified = FALSE,
            updated_at = NOW()
        WHERE provider_name = :provider_name
          AND target_reference_id IS NOT NULL
          AND match_score BETWEEN :score_min AND :score_max
        """
    )
    flagged_count = 0
    if not dry_run:
        with SessionLocal() as session:
            flagged = session.execute(
                flagged_low_conf_query,
                {
                    "provider_name": provider,
                    "score_min": review_score_min,
                    "score_max": review_score_max,
                },
            )
            session.commit()
            flagged_count = int(flagged.rowcount or 0)
            print(
                f"Flagged for review (is_verified=false): "
                f"{flagged_count} rows in score range "
                f"[{review_score_min}, {review_score_max}]"
            )

    top_orphans = top_unlinked_source_names(provider_name=provider, top_n=report_top_unlinked)
    print("Top unlinked source names:")
    for row in top_orphans:
        print(f"  - {row['source_norm'] or '[empty]'}: {int(row['cnt'])}")

    mapped_rows, high_conf_rows = high_confidence_stats(provider_name=provider)
    high_conf_pct = (high_conf_rows / mapped_rows * 100.0) if mapped_rows else 0.0
    print(f"High-confidence mapping rows: {high_conf_rows}/{mapped_rows} ({high_conf_pct:.2f}%)")

    return {
        "provider": provider,
        "seeded_mappings": seeded,
        "filled_mapping_targets": filled,
        "backfilled_scores": backfilled,
        "relinked_listings": relinked,
        "needs_mapping_inserted": needs_mapping,
        "total_ads": total_ads,
        "linked_ads": linked_ads,
        "orphan_ads": orphan_ads,
        "coverage_percent": round(coverage, 2),
        "flagged_low_confidence": flagged_count,
        "mapped_rows": mapped_rows,
        "high_confidence_rows": high_conf_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Relink listings using mapping_provider_data only.")
    parser.add_argument("--provider", default="divar")
    parser.add_argument("--fill-gap-top-n", type=int, default=20)
    parser.add_argument("--min-score", type=int, default=72)
    parser.add_argument("--report-top-unlinked", type=int, default=15)
    parser.add_argument("--needs-mapping-top", type=int, default=20)
    parser.add_argument("--review-score-min", type=int, default=58)
    parser.add_argument("--review-score-max", type=int, default=68)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-reference-seeds",
        action="store_true",
        help="Skip MANUAL_REFERENCE_SEEDS / ensure_car_reference (never insert car_reference from this run).",
    )
    args = parser.parse_args()

    run_relink_pipeline(
        provider=args.provider,
        fill_gap_top_n=args.fill_gap_top_n,
        min_score=args.min_score,
        report_top_unlinked=args.report_top_unlinked,
        needs_mapping_top=args.needs_mapping_top,
        review_score_min=args.review_score_min,
        review_score_max=args.review_score_max,
        dry_run=args.dry_run,
        allow_car_reference_mutations=not args.no_reference_seeds,
    )


if __name__ == "__main__":
    main()
