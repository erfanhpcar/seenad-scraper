from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


def _column_exists(session, column_name: str) -> bool:
    probe = text(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'scraped_listings'
          AND column_name = :column_name
        LIMIT 1
        """
    )
    return session.execute(probe, {"column_name": column_name}).first() is not None


def fetch_counts(
    session,
    logical_name: str,
    direct_column: str,
    json_key_fallback: str,
) -> tuple[list[dict], str]:
    if _column_exists(session, direct_column):
        query = text(
            f"""
            SELECT {direct_column} AS value, COUNT(*) AS total_count
            FROM scraped_listings
            GROUP BY {direct_column}
            ORDER BY total_count DESC, value NULLS LAST
            """
        )
        rows = session.execute(query).mappings().all()
        source = f"scraped_listings.{direct_column}"
    else:
        query = text(
            """
            SELECT raw_data ->> :json_key AS value, COUNT(*) AS total_count
            FROM scraped_listings
            GROUP BY raw_data ->> :json_key
            ORDER BY total_count DESC, value NULLS LAST
            """
        )
        rows = session.execute(query, {"json_key": json_key_fallback}).mappings().all()
        source = f"scraped_listings.raw_data->>'{json_key_fallback}' (fallback for {logical_name})"
    return rows, source


def print_counts(rows: list[dict], logical_name: str, source: str) -> None:
    print("=" * 100)
    print(f"Unique values for `{logical_name}` (sorted by count desc)")
    print(f"Source: {source}")
    print("=" * 100)
    for row in rows:
        print(f"{row['total_count']:>6} | {repr(row['value'])}")
    print(f"\nTotal unique values in `{logical_name}`: {len(rows)}\n")


def write_tsv(path: Path, rows: list[dict], logical_name: str, source: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"logical_name\t{logical_name}\n")
        handle.write(f"source\t{source}\n")
        handle.write("count\tvalue\n")
        for row in rows:
            value = "" if row["value"] is None else str(row["value"])
            handle.write(f"{row['total_count']}\t{value}\n")


def main() -> None:
    with SessionLocal() as session:
        brand_rows, brand_source = fetch_counts(
            session=session,
            logical_name="brand_name",
            direct_column="brand_name",
            json_key_fallback="brand",
        )
        model_rows, model_source = fetch_counts(
            session=session,
            logical_name="model_name",
            direct_column="model_name",
            json_key_fallback="tip",
        )

    print_counts(brand_rows, "brand_name", brand_source)
    print_counts(model_rows, "model_name", model_source)

    output_dir = Path(__file__).resolve().parents[1] / "docs"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_tsv(output_dir / "brand_name_counts.tsv", brand_rows, "brand_name", brand_source)
    write_tsv(output_dir / "model_name_counts.tsv", model_rows, "model_name", model_source)
    print(f"Saved: {output_dir / 'brand_name_counts.tsv'}")
    print(f"Saved: {output_dir / 'model_name_counts.tsv'}")


if __name__ == "__main__":
    main()
