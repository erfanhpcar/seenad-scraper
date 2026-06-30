"""drop_redundant_mapping_tables

Revision ID: drop_redundant_mapping_tables
Revises:
Create Date: 2026-06-03

Phase C: Scraper Server DB cleanup — drop catalog/mapping tables and scraped_listings FK columns.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "drop_redundant_mapping_tables"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS listings_clean CASCADE")
    op.execute("DROP TABLE IF EXISTS car_zero_price CASCADE")
    op.execute("DROP TABLE IF EXISTS mapping_provider_data CASCADE")
    op.execute("DROP TABLE IF EXISTS mapping_pipeline_history CASCADE")

    op.execute(
        "ALTER TABLE scraped_listings DROP CONSTRAINT IF EXISTS scraped_listings_car_reference_id_fkey"
    )
    op.execute("ALTER TABLE scraped_listings DROP CONSTRAINT IF EXISTS scraped_listings_color_id_fkey")
    op.execute(
        "ALTER TABLE scraped_listings DROP CONSTRAINT IF EXISTS scraped_listings_gearbox_id_fkey"
    )

    op.execute("DROP INDEX IF EXISTS ix_scraped_listings_car_reference_id")
    op.execute("DROP INDEX IF EXISTS ix_scraped_listings_color_id")
    op.execute("DROP INDEX IF EXISTS ix_scraped_listings_gearbox_id")

    op.execute("ALTER TABLE scraped_listings DROP COLUMN IF EXISTS car_reference_id")
    op.execute("ALTER TABLE scraped_listings DROP COLUMN IF EXISTS color_id")
    op.execute("ALTER TABLE scraped_listings DROP COLUMN IF EXISTS gearbox_id")

    op.execute("DROP TABLE IF EXISTS colors CASCADE")
    op.execute("DROP TABLE IF EXISTS gearbox_types CASCADE")
    op.execute("DROP TABLE IF EXISTS car_entities CASCADE")


def downgrade() -> None:
    raise NotImplementedError(
        "downgrade for drop_redundant_mapping_tables is not supported; restore from backup."
    )
