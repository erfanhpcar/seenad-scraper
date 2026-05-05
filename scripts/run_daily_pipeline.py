from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_normalizer import run_normalization
from app.database import SessionLocal


def ensure_pipeline_history_table() -> None:
    query = text(
        """
        CREATE TABLE IF NOT EXISTS mapping_pipeline_history (
            id BIGSERIAL PRIMARY KEY,
            provider_name VARCHAR(50) NOT NULL,
            run_started_at TIMESTAMPTZ NOT NULL,
            run_finished_at TIMESTAMPTZ NOT NULL,
            total_ads INTEGER NOT NULL,
            linked_ads INTEGER NOT NULL,
            orphan_ads INTEGER NOT NULL,
            coverage_percent NUMERIC(5,2) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    with SessionLocal() as session:
        session.execute(query)
        session.commit()


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


def store_pipeline_history(
    provider_name: str,
    started_at: datetime,
    finished_at: datetime,
    total_ads: int,
    linked_ads: int,
    metadata: dict[str, object],
) -> None:
    orphan_ads = total_ads - linked_ads
    coverage = (linked_ads / total_ads * 100.0) if total_ads else 0.0
    query = text(
        """
        INSERT INTO mapping_pipeline_history (
            provider_name,
            run_started_at,
            run_finished_at,
            total_ads,
            linked_ads,
            orphan_ads,
            coverage_percent,
            metadata
        )
        VALUES (
            :provider_name,
            :run_started_at,
            :run_finished_at,
            :total_ads,
            :linked_ads,
            :orphan_ads,
            :coverage_percent,
            CAST(:metadata AS JSONB)
        )
        """
    )
    with SessionLocal() as session:
        session.execute(
            query,
            {
                "provider_name": provider_name,
                "run_started_at": started_at,
                "run_finished_at": finished_at,
                "total_ads": total_ads,
                "linked_ads": linked_ads,
                "orphan_ads": orphan_ads,
                "coverage_percent": round(coverage, 2),
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )
        session.commit()


def run_relink_script(args: argparse.Namespace) -> None:
    script_path = Path(__file__).resolve().parents[0] / "relink_from_mapping.py"
    command = [
        sys.executable,
        str(script_path),
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
        "--needs-mapping-top",
        str(args.needs_mapping_top),
    ]
    if args.dry_run:
        command.append("--dry-run")
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily data pipeline: scrape -> relink -> persist coverage.")
    parser.add_argument("--provider", default="divar")
    parser.add_argument("--skip-scraper", action="store_true")
    parser.add_argument("--normalizer-limit", type=int, default=0)
    parser.add_argument("--fill-gap-top-n", type=int, default=20)
    parser.add_argument("--min-score", type=int, default=58)
    parser.add_argument("--review-score-min", type=int, default=58)
    parser.add_argument("--review-score-max", type=int, default=68)
    parser.add_argument("--report-top-unlinked", type=int, default=20)
    parser.add_argument("--needs-mapping-top", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", default="logs/daily_pipeline.log")
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc)
    ensure_pipeline_history_table()

    metadata: dict[str, object] = {
        "scraper_step": "skipped" if args.skip_scraper else "completed",
        "relink_step": "pending",
        "normalizer_limit": args.normalizer_limit,
        "dry_run": args.dry_run,
    }

    if not args.skip_scraper:
        limit = args.normalizer_limit if args.normalizer_limit > 0 else None
        run_normalization(limit_per_table=limit, incremental=True)

    run_relink_script(args)
    metadata["relink_step"] = "completed"

    total_ads, linked_ads = provider_coverage(args.provider)
    finished_at = datetime.now(timezone.utc)
    store_pipeline_history(
        provider_name=args.provider,
        started_at=started_at,
        finished_at=finished_at,
        total_ads=total_ads,
        linked_ads=linked_ads,
        metadata=metadata,
    )

    orphan_ads = total_ads - linked_ads
    coverage = (linked_ads / total_ads * 100.0) if total_ads else 0.0
    log_payload = {
        "provider": args.provider,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "total_ads": total_ads,
        "linked_ads": linked_ads,
        "orphan_ads": orphan_ads,
        "coverage_percent": round(coverage, 2),
        "metadata": metadata,
    }
    log_path = Path(__file__).resolve().parents[1] / args.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(log_payload, ensure_ascii=False) + os.linesep)

    print(json.dumps(log_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
