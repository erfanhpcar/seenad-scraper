from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.car_normalization import normalize_brand_model
from app.database import SessionLocal


def ensure_columns() -> None:
    statements = [
        "ALTER TABLE scraped_listings ADD COLUMN IF NOT EXISTS brand_name VARCHAR(100)",
        "ALTER TABLE scraped_listings ADD COLUMN IF NOT EXISTS model_name VARCHAR(120)",
        "ALTER TABLE scraped_listings ADD COLUMN IF NOT EXISTS trim_name VARCHAR(120)",
        "CREATE INDEX IF NOT EXISTS ix_scraped_listings_brand_name ON scraped_listings(brand_name)",
        "CREATE INDEX IF NOT EXISTS ix_scraped_listings_model_name ON scraped_listings(model_name)",
    ]
    with SessionLocal() as session:
        for stmt in statements:
            session.execute(text(stmt))
        session.commit()


def backfill(batch_size: int = 1000) -> None:
    with SessionLocal() as session:
        total = session.execute(text("SELECT COUNT(*) FROM scraped_listings")).scalar_one()
        updated = 0
        offset = 0
        while offset < total:
            rows = session.execute(
                text(
                    """
                    SELECT id, raw_data->>'brand' AS brand_raw, raw_data->>'tip' AS tip_raw
                    FROM scraped_listings
                    ORDER BY id
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"limit": batch_size, "offset": offset},
            ).mappings().all()
            if not rows:
                break

            payload = []
            for row in rows:
                normalized = normalize_brand_model(row["brand_raw"], row["tip_raw"])
                payload.append(
                    {
                        "id": row["id"],
                        "brand_name": normalized.brand_name,
                        "model_name": normalized.model_name,
                        "trim_name": normalized.trim_name,
                    }
                )

            session.execute(
                text(
                    """
                    UPDATE scraped_listings
                    SET brand_name = :brand_name,
                        model_name = :model_name,
                        trim_name = :trim_name
                    WHERE id = :id
                    """
                ),
                payload,
            )
            session.commit()
            updated += len(payload)
            offset += len(payload)
            print(f"Processed {updated}/{total}")


def main() -> None:
    ensure_columns()
    backfill()
    print("Normalization migration completed.")


if __name__ == "__main__":
    main()
