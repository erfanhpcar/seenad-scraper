from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deprecated wrapper for backward compatibility. Forwards to relink_from_mapping.py."
    )
    parser.add_argument("--provider", default="divar")
    parser.add_argument("--fill-gap-top-n", type=int, default=20)
    parser.add_argument("--min-score", type=int, default=58)
    parser.add_argument("--review-score-min", type=int, default=58)
    parser.add_argument("--review-score-max", type=int, default=68)
    parser.add_argument("--report-top-unlinked", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    relink_script = root / "scripts" / "relink_from_mapping.py"
    command = [
        sys.executable,
        str(relink_script),
        "--provider",
        args.provider,
        "--fill-gap-top-n",
        str(args.fill_gap_top_n),
        "--min-score",
        str(args.min_score),
        "--review-score-min",
        str(args.review_score_min),
        "--review-score-max",
        str(args.review_score_max),
        "--report-top-unlinked",
        str(args.report_top_unlinked),
    ]
    if args.dry_run:
        command.append("--dry-run")

    print("[wrapper] unify_car_reference.py is deprecated.")
    print(f"[wrapper] forwarding to {relink_script}")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


@dataclass
class CarReferenceRow:
    id: int
    brand: str
    model: str
    trim: str
    source_url: str
    model_norm: str
    trim_norm: str
    key_norm: str


@dataclass
class MergePlan:
    legacy_id: int
    legacy_brand: str
    master_id: int
    master_brand: str
    score: int


@dataclass
class ListingRelinkRule:
    source_brand: str
    target_brand: str
    target_model: str
    source_model: str = ""
    target_trim: str = "Base"


@dataclass
class MappingSeed:
    source_string: str
    provider_name: str
    target_brand: str
    target_model: str
    target_trim: str = "Base"


BRAND_MAP: dict[str, str] = {
    "peugeot": "پژو",
    "pride": "پراید",
    "samand": "سمند",
}
LISTING_RELINK_RULES: list[ListingRelinkRule] = [
    ListingRelinkRule(source_brand="jac", source_model="s5", target_brand="جک", target_model="S5"),
    ListingRelinkRule(source_brand="dena", source_model="plus", target_brand="دنا", target_model="پلاس"),
    ListingRelinkRule(source_brand="peugeot", source_model="roa", target_brand="پژو", target_model="روا"),
    ListingRelinkRule(source_brand="شاهین g", target_brand="شاهین", target_model="G"),
    ListingRelinkRule(source_brand="تارا v1 پلاس", target_brand="تارا", target_model="V1 Plus"),
    ListingRelinkRule(source_brand="تیبا 2 (هاچبک) ex", target_brand="تیبا", target_model="تیبا ۲", target_trim="EX"),
    ListingRelinkRule(source_brand="رنو تندر 90 (l90 لوگان) e2", target_brand="رنو", target_model="تندر ۹۰", target_trim="E2"),
    ListingRelinkRule(source_brand="پیکان وانت cng", target_brand="پیکان", target_model="وانت", target_trim="CNG"),
    ListingRelinkRule(source_brand="وانت آریسان 2 دوگانه سوز", target_brand="آریسان", target_model="آریسان ۲", target_trim="دوگانه‌سوز"),
]
MAPPING_SEEDS: list[MappingSeed] = [
    MappingSeed(source_string="jac s5", provider_name="divar", target_brand="جک", target_model="S5"),
    MappingSeed(source_string="dena plus", provider_name="divar", target_brand="دنا", target_model="پلاس"),
    MappingSeed(source_string="peugeot roa", provider_name="divar", target_brand="پژو", target_model="روا"),
    MappingSeed(source_string="شاهین g", provider_name="divar", target_brand="شاهین", target_model="G"),
    MappingSeed(source_string="تارا v1 پلاس", provider_name="divar", target_brand="تارا", target_model="V1 Plus"),
    MappingSeed(source_string="تیبا 2 (هاچبک) ex", provider_name="divar", target_brand="تیبا", target_model="تیبا ۲", target_trim="EX"),
    MappingSeed(
        source_string="رنو تندر 90 (l90 لوگان) e2",
        provider_name="divar",
        target_brand="رنو",
        target_model="تندر ۹۰",
        target_trim="E2",
    ),
    MappingSeed(source_string="پیکان وانت cng", provider_name="divar", target_brand="پیکان", target_model="وانت", target_trim="CNG"),
    MappingSeed(
        source_string="وانت آریسان 2 دوگانه سوز",
        provider_name="divar",
        target_brand="آریسان",
        target_model="آریسان ۲",
        target_trim="دوگانه‌سوز",
    ),
]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS).replace("\u200c", " ").strip().lower()
    cleaned = NON_WORD_RE.sub(" ", cleaned)
    return SPACE_RE.sub(" ", cleaned).strip()


def ensure_mapping_provider_table() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS mapping_provider_data (
            id BIGSERIAL PRIMARY KEY,
            provider_name VARCHAR(50) NOT NULL,
            source_string VARCHAR(300) NOT NULL,
            target_reference_id INTEGER NULL REFERENCES car_entities(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider_name, source_string)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_mapping_provider_data_provider_target
        ON mapping_provider_data(provider_name, target_reference_id)
        """,
    ]
    with SessionLocal() as session:
        for stmt in statements:
            session.execute(text(stmt))
        session.commit()


def score_similarity(a: str, b: str) -> int:
    if not a or not b:
        return 0
    return int(SequenceMatcher(None, a, b).ratio() * 100)


def load_references_by_brand(brand: str) -> list[CarReferenceRow]:
    brand_norm = normalize_text(brand)
    query = text(
        """
        SELECT id, brand, model, trim, source_url
        FROM car_entities
        WHERE lower(btrim(brand)) = :brand_norm
        ORDER BY id
        """
    )
    with SessionLocal() as session:
        rows = session.execute(query, {"brand_norm": brand_norm}).mappings().all()
    return [
        CarReferenceRow(
            id=int(row["id"]),
            brand=str(row["brand"]),
            model=str(row["model"]),
            trim=str(row["trim"]),
            source_url=str(row["source_url"] or ""),
            model_norm=normalize_text(str(row["model"])),
            trim_norm=normalize_text(str(row["trim"])),
            key_norm=normalize_text(f"{row['model']} {row['trim']}"),
        )
        for row in rows
    ]


def pick_master_row(legacy: CarReferenceRow, masters: list[CarReferenceRow], min_score: int) -> tuple[CarReferenceRow | None, int]:
    if not masters:
        return None, -1

    # Fast path: exact normalized model+trim
    for candidate in masters:
        if legacy.key_norm and legacy.key_norm == candidate.key_norm:
            return candidate, 100

    # Fallback: best fuzzy similarity on model+trim.
    best: CarReferenceRow | None = None
    best_score = -1
    for candidate in masters:
        score = score_similarity(legacy.key_norm, candidate.key_norm)
        if score > best_score:
            best = candidate
            best_score = score

    if best is None or best_score < min_score:
        return None, best_score
    return best, best_score


def count_listings_for_reference(reference_id: int) -> int:
    query = text("SELECT COUNT(*) FROM scraped_listings WHERE car_reference_id = :reference_id")
    with SessionLocal() as session:
        count = session.execute(query, {"reference_id": reference_id}).scalar_one()
    return int(count)


def ensure_target_reference(target_brand: str, target_model: str, target_trim: str) -> int:
    target_brand_norm = normalize_text(target_brand)
    target_model_norm = normalize_text(target_model)
    target_trim_norm = normalize_text(target_trim)

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
        VALUES (:target_brand, :target_model, :target_trim, :source_url, NULL, NULL)
        ON CONFLICT (brand, model, trim)
        DO UPDATE SET source_url = EXCLUDED.source_url
        RETURNING id
        """
    )

    with SessionLocal() as session:
        existing = session.execute(
            select_query,
            {
                "brand_norm": target_brand_norm,
                "model_norm": target_model_norm,
                "trim_norm": target_trim_norm,
            },
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)

        created_id = session.execute(
            insert_query,
            {
                "target_brand": target_brand,
                "target_model": target_model,
                "target_trim": target_trim,
                "source_url": "legacy-unify-rule",
            },
        ).scalar_one()
        session.commit()
        print(
            f"[create-master] created car_reference id={int(created_id)} "
            f"brand={target_brand!r} model={target_model!r} trim={target_trim!r}"
        )
        return int(created_id)


def relink_unlinked_listings_by_rules(dry_run: bool) -> int:
    total_relinked = 0
    count_query = text(
        """
        SELECT COUNT(*)
        FROM scraped_listings sl
        WHERE sl.car_reference_id IS NULL
          AND lower(btrim(sl.brand_name)) = :source_brand
          AND (
                :source_model = ''
                OR lower(btrim(translate(COALESCE(sl.model_name, ''), '۰۱۲۳۴۵۶۷۸۹', '0123456789'))) = :source_model
              )
        """
    )
    update_query = text(
        """
        UPDATE scraped_listings sl
        SET car_reference_id = :target_reference_id
        WHERE sl.car_reference_id IS NULL
          AND lower(btrim(sl.brand_name)) = :source_brand
          AND (
                :source_model = ''
                OR lower(btrim(translate(COALESCE(sl.model_name, ''), '۰۱۲۳۴۵۶۷۸۹', '0123456789'))) = :source_model
              )
        """
    )

    for rule in LISTING_RELINK_RULES:
        source_brand_norm = str(rule.source_brand).translate(PERSIAN_DIGITS).replace("\u200c", " ").strip().lower()
        source_model_norm = str(rule.source_model).translate(PERSIAN_DIGITS).replace("\u200c", " ").strip().lower()
        affected_count = 0
        with SessionLocal() as session:
            affected_count = int(
                session.execute(
                    count_query,
                    {
                        "source_brand": source_brand_norm,
                        "source_model": source_model_norm,
                    },
                ).scalar_one()
            )

        if affected_count <= 0:
            continue

        target_reference_id = ensure_target_reference(rule.target_brand, rule.target_model, rule.target_trim)
        if dry_run:
            total_relinked += affected_count
            print(
                f"[rule dry-run] {rule.source_brand}/{rule.source_model} -> "
                f"{rule.target_brand}/{rule.target_model}/{rule.target_trim} "
                f"target_id={target_reference_id} relink_candidates={affected_count}"
            )
            continue

        with SessionLocal() as session:
            updated = session.execute(
                update_query,
                {
                    "target_reference_id": target_reference_id,
                    "source_brand": source_brand_norm,
                    "source_model": source_model_norm,
                },
            )
            session.commit()
            changed = int(updated.rowcount or 0)
            total_relinked += changed
            print(
                f"[rule apply] {rule.source_brand}/{rule.source_model} -> "
                f"{rule.target_brand}/{rule.target_model}/{rule.target_trim} "
                f"target_id={target_reference_id} relinked={changed}"
            )

    return total_relinked


def seed_mapping_provider_data(dry_run: bool) -> int:
    upsert_query = text(
        """
        INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id)
        VALUES (:provider_name, :source_string, :target_reference_id)
        ON CONFLICT (provider_name, source_string)
        DO UPDATE SET
            target_reference_id = EXCLUDED.target_reference_id,
            updated_at = NOW()
        """
    )
    seeded = 0
    for seed in MAPPING_SEEDS:
        target_reference_id = ensure_target_reference(seed.target_brand, seed.target_model, seed.target_trim)
        if dry_run:
            seeded += 1
            continue
        with SessionLocal() as session:
            session.execute(
                upsert_query,
                {
                    "provider_name": seed.provider_name,
                    "source_string": normalize_text(seed.source_string),
                    "target_reference_id": target_reference_id,
                },
            )
            session.commit()
            seeded += 1
    return seeded


def relink_unlinked_listings_via_mapping_table(provider_name: str, dry_run: bool) -> int:
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
        WHERE sl.car_reference_id IS NULL
          AND lower(sl.source) LIKE :provider_prefix
        """
    )
    update_query = text(
        """
        UPDATE scraped_listings sl
        SET car_reference_id = mpd.target_reference_id
        FROM mapping_provider_data mpd
        WHERE mpd.provider_name = :provider_name
          AND mpd.target_reference_id IS NOT NULL
          AND sl.car_reference_id IS NULL
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
        """
    )
    params = {
        "provider_name": provider_name,
        "provider_prefix": f"{provider_name}%",
    }
    with SessionLocal() as session:
        planned = int(session.execute(count_query, params).scalar_one())
    if dry_run:
        return planned
    with SessionLocal() as session:
        result = session.execute(update_query, params)
        session.commit()
        return int(result.rowcount or 0)


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
    insert_placeholder_query = text(
        """
        INSERT INTO mapping_provider_data (provider_name, source_string, target_reference_id)
        VALUES (:provider_name, :source_string, NULL)
        ON CONFLICT (provider_name, source_string)
        DO NOTHING
        """
    )
    params = {
        "provider_name": provider_name,
        "provider_prefix": f"{provider_name}%",
        "top_n": top_n,
    }
    with SessionLocal() as session:
        rows = session.execute(missing_query, params).mappings().all()
    if rows:
        print(f"[needs-mapping:{provider_name}] top {len(rows)}")
        for row in rows:
            print(f"  - {row['source_norm']} | unlinked={int(row['cnt'])}")
    if dry_run:
        return len(rows)
    inserted = 0
    with SessionLocal() as session:
        for row in rows:
            result = session.execute(
                insert_placeholder_query,
                {
                    "provider_name": provider_name,
                    "source_string": str(row["source_norm"]),
                },
            )
            inserted += int(result.rowcount or 0)
        session.commit()
    return inserted


def build_merge_plans(english_brand: str, persian_brand: str, min_score: int) -> tuple[list[MergePlan], list[int]]:
    legacy_rows = load_references_by_brand(english_brand)
    master_rows = load_references_by_brand(persian_brand)
    plans: list[MergePlan] = []
    unmatched_ids: list[int] = []

    if not legacy_rows:
        print(f"[{english_brand}] no legacy rows found after case-insensitive/trim lookup")
    if not master_rows:
        print(f"[{english_brand} -> {persian_brand}] no master rows found; merge is impossible")

    for legacy in legacy_rows:
        master, score = pick_master_row(legacy=legacy, masters=master_rows, min_score=min_score)
        if master is None:
            unmatched_ids.append(legacy.id)
            legacy_ads = count_listings_for_reference(legacy.id)
            if not master_rows:
                print(
                    f"  ! skip legacy_id={legacy.id} brand={legacy.brand!r} "
                    f"model={legacy.model!r} trim={legacy.trim!r} reason=no_master_brand_rows listings={legacy_ads}"
                )
            else:
                print(
                    f"  ! skip legacy_id={legacy.id} brand={legacy.brand!r} "
                    f"model={legacy.model!r} trim={legacy.trim!r} reason=no_match_above_threshold "
                    f"best_score={score} min_score={min_score} listings={legacy_ads}"
                )
            continue
        plans.append(
            MergePlan(
                legacy_id=legacy.id,
                legacy_brand=english_brand,
                master_id=master.id,
                master_brand=persian_brand,
                score=score,
            )
        )

    return plans, unmatched_ids


def apply_plans(plans: list[MergePlan], rename_only_ids: list[int], rename_to: str, dry_run: bool) -> tuple[int, int, int]:
    relinked = 0
    deleted = 0
    renamed = 0

    if dry_run:
        for plan in plans:
            relinked += count_listings_for_reference(plan.legacy_id)
            deleted += 1
        renamed += len(rename_only_ids)
        return relinked, deleted, renamed

    relink_query = text(
        """
        UPDATE scraped_listings
        SET car_reference_id = :master_id
        WHERE car_reference_id = :legacy_id
        """
    )
    delete_query = text("DELETE FROM car_entities WHERE id = :legacy_id")
    rename_query = text(
        """
        UPDATE car_entities
        SET brand = :rename_to
        WHERE id = :row_id
        """
    )

    with SessionLocal() as session:
        for plan in plans:
            relink_result = session.execute(
                relink_query,
                {"master_id": plan.master_id, "legacy_id": plan.legacy_id},
            )
            relinked += int(relink_result.rowcount or 0)

            delete_result = session.execute(delete_query, {"legacy_id": plan.legacy_id})
            deleted += int(delete_result.rowcount or 0)

        for row_id in rename_only_ids:
            rename_result = session.execute(rename_query, {"rename_to": rename_to, "row_id": row_id})
            renamed += int(rename_result.rowcount or 0)

        session.commit()

    return relinked, deleted, renamed


def cleanup_orphan_english_brands(dry_run: bool) -> int:
    english_brands = tuple(BRAND_MAP.keys())
    cleanup_query = text(
        """
        DELETE FROM car_entities cr
        WHERE lower(btrim(cr.brand)) = ANY(:english_brands)
          AND NOT EXISTS (
              SELECT 1
              FROM scraped_listings sl
              WHERE sl.car_reference_id = cr.id
          )
        """
    )
    count_query = text(
        """
        SELECT COUNT(*)
        FROM car_entities cr
        WHERE lower(btrim(cr.brand)) = ANY(:english_brands)
          AND NOT EXISTS (
              SELECT 1
              FROM scraped_listings sl
              WHERE sl.car_reference_id = cr.id
          )
        """
    )
    with SessionLocal() as session:
        if dry_run:
            return int(session.execute(count_query, {"english_brands": list(english_brands)}).scalar_one())
        deleted = session.execute(cleanup_query, {"english_brands": list(english_brands)})
        session.commit()
        return int(deleted.rowcount or 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unify English legacy car_reference brands into Persian master brands and relink scraped listings."
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=85,
        help="Minimum fuzzy similarity for model+trim merge when exact match is missing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without writing changes.",
    )
    parser.add_argument("--provider", type=str, default="divar")
    parser.add_argument("--needs-mapping-top", type=int, default=20)
    args = parser.parse_args()

    ensure_mapping_provider_table()
    total_plans = 0
    total_unmatched = 0
    total_relinked = 0
    total_deleted = 0
    total_renamed = 0

    for english_brand, persian_brand in BRAND_MAP.items():
        plans, unmatched_ids = build_merge_plans(
            english_brand=english_brand,
            persian_brand=persian_brand,
            min_score=args.min_score,
        )
        relinked, deleted, renamed = apply_plans(
            plans=plans,
            rename_only_ids=unmatched_ids,
            rename_to=persian_brand,
            dry_run=args.dry_run,
        )
        total_plans += len(plans)
        total_unmatched += len(unmatched_ids)
        total_relinked += relinked
        total_deleted += deleted
        total_renamed += renamed
        print(
            f"[{english_brand} -> {persian_brand}] merges={len(plans)} "
            f"renamed_unmatched={len(unmatched_ids)} relinked_ads={relinked} deleted_legacy={deleted}"
        )

    orphan_deleted = cleanup_orphan_english_brands(dry_run=args.dry_run)
    rule_relinked = relink_unlinked_listings_by_rules(dry_run=args.dry_run)
    seeded_mappings = seed_mapping_provider_data(dry_run=args.dry_run)
    table_relinked = relink_unlinked_listings_via_mapping_table(provider_name=args.provider, dry_run=args.dry_run)
    needs_mapping_inserted = collect_and_store_needs_mapping(
        provider_name=args.provider,
        top_n=args.needs_mapping_top,
        dry_run=args.dry_run,
    )
    total_relinked += rule_relinked
    total_relinked += table_relinked

    print("----")
    print(f"mode={'DRY-RUN' if args.dry_run else 'APPLY'}")
    print(f"total_merges={total_plans}")
    print(f"total_unmatched_renamed={total_unmatched}")
    print(f"total_relinked_ads={total_relinked}")
    print(f"total_deleted_legacy_rows={total_deleted}")
    print(f"total_renamed_rows={total_renamed}")
    print(f"orphan_english_rows_deleted={orphan_deleted}")
    print(f"rule_based_unlinked_relinked={rule_relinked}")
    print(f"mapping_rows_seeded={seeded_mappings}")
    print(f"mapping_table_relinked={table_relinked}")
    print(f"needs_mapping_rows_inserted={needs_mapping_inserted}")


if __name__ == "__main__":
    main()
