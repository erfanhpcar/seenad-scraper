"""PostgreSQL integration tests for zero_price_scrape_batch lifecycle."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal, engine
from app.models import CarZeroPriceScrapeSnapshot, IranJibZeroPriceScrapeSnapshot, ZeroPriceScrapeBatch
from src.scraper.zero_price_batch_registry import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    ZeroPriceBatchInvalidCompletionError,
    ZeroPriceBatchSource,
    abort_zero_price_batch,
    begin_zero_price_batch,
    complete_zero_price_batch,
    count_snapshot_rows_for_batch,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_022 = REPO_ROOT / "postgres" / "migrations" / "022_zero_price_scrape_batch.sql"


def _db_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def _ensure_registry_table() -> None:
    sql = MIGRATION_022.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))


def _new_batch_id() -> str:
    return f"test-zp-{uuid.uuid4().hex[:16]}"


def _insert_bama_snapshot(db, *, batch_id: str, bama_car_price_id: int) -> None:
    db.add(
        CarZeroPriceScrapeSnapshot(
            batch_id=batch_id,
            hierarchy_page_index=0,
            brand_name_raw="test",
            brand_slug_raw="test",
            model_name_raw="test",
            model_slug_raw="test",
            trim_name_raw="test",
            trim_slug_raw="test",
            model_year_raw="1403",
            price_raw="1000",
            bama_car_price_id=bama_car_price_id,
        )
    )


def _insert_iranjib_snapshot(db, *, batch_id: str, iranjib_iid: int) -> None:
    db.add(
        IranJibZeroPriceScrapeSnapshot(
            batch_id=batch_id,
            page_kind="domestic",
            page_url="https://iranjib.ir/test",
            section_raw="test",
            title_raw="test",
            iranjib_iid=iranjib_iid,
            market_price_raw="1000",
            factory_price_raw="900",
            change_amount_raw="0",
            change_percent_raw="0",
            status_raw="",
            status="UNKNOWN",
            market_price_toman=1000,
            factory_price_toman=900,
        )
    )


@unittest.skipUnless(
    os.getenv("ZERO_PRICE_BATCH_PG_TESTS", "1").strip().lower() not in {"0", "false", "no"},
    "ZERO_PRICE_BATCH_PG_TESTS disabled",
)
@unittest.skipUnless(_db_reachable(), "PostgreSQL is not reachable for integration tests")
class ZeroPriceBatchRegistryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_registry_table()

    def _cleanup_batch(self, batch_id: str) -> None:
        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM car_zero_price_scrape_snapshot WHERE batch_id = :bid"),
                {"bid": batch_id},
            )
            db.execute(
                text("DELETE FROM iranjib_zero_price_scrape_snapshot WHERE batch_id = :bid"),
                {"bid": batch_id},
            )
            db.execute(
                text("DELETE FROM zero_price_scrape_batch WHERE batch_id = :bid"),
                {"bid": batch_id},
            )
            db.commit()

    def test_begin_leaves_in_progress(self) -> None:
        batch_id = _new_batch_id()
        try:
            with SessionLocal() as db:
                begin_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id=batch_id)
                db.commit()
                row = db.get(ZeroPriceScrapeBatch, batch_id)
                assert row is not None
                self.assertEqual(row.status, STATUS_IN_PROGRESS)
                self.assertIsNone(row.completed_at)
        finally:
            self._cleanup_batch(batch_id)

    def test_complete_after_snapshots_marks_completed(self) -> None:
        batch_id = _new_batch_id()
        try:
            with SessionLocal() as db:
                begin_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id=batch_id)
                _insert_bama_snapshot(db, batch_id=batch_id, bama_car_price_id=900_000_001)
                db.commit()
                complete_zero_price_batch(
                    db,
                    source=ZeroPriceBatchSource.BAMA,
                    batch_id=batch_id,
                    raw_row_count=1,
                )
                db.commit()
                row = db.get(ZeroPriceScrapeBatch, batch_id)
                self.assertEqual(row.status, STATUS_COMPLETED)
                self.assertEqual(row.raw_row_count, 1)
                self.assertIsNotNone(row.completed_at)
                self.assertEqual(
                    count_snapshot_rows_for_batch(
                        db, source=ZeroPriceBatchSource.BAMA, batch_id=batch_id
                    ),
                    1,
                )
        finally:
            self._cleanup_batch(batch_id)

    def test_abort_marks_failed_and_removes_snapshots(self) -> None:
        batch_id = _new_batch_id()
        try:
            with SessionLocal() as db:
                begin_zero_price_batch(db, source=ZeroPriceBatchSource.IRANJIB, batch_id=batch_id)
                _insert_iranjib_snapshot(db, batch_id=batch_id, iranjib_iid=800_000_001)
                db.commit()
            with SessionLocal() as db:
                abort_zero_price_batch(db, source=ZeroPriceBatchSource.IRANJIB, batch_id=batch_id)
                db.commit()
                row = db.get(ZeroPriceScrapeBatch, batch_id)
                self.assertEqual(row.status, STATUS_FAILED)
                self.assertEqual(
                    count_snapshot_rows_for_batch(
                        db, source=ZeroPriceBatchSource.IRANJIB, batch_id=batch_id
                    ),
                    0,
                )
        finally:
            self._cleanup_batch(batch_id)

    def test_empty_batch_never_becomes_completed(self) -> None:
        batch_id = _new_batch_id()
        try:
            with SessionLocal() as db:
                begin_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id=batch_id)
                db.commit()
            with SessionLocal() as db:
                with self.assertRaises(ZeroPriceBatchInvalidCompletionError):
                    complete_zero_price_batch(
                        db,
                        source=ZeroPriceBatchSource.BAMA,
                        batch_id=batch_id,
                        raw_row_count=0,
                    )
                row = db.get(ZeroPriceScrapeBatch, batch_id)
                self.assertEqual(row.status, STATUS_IN_PROGRESS)
                self.assertIsNone(row.completed_at)
        finally:
            self._cleanup_batch(batch_id)

    def test_worker_kill_leaves_in_progress_not_consumable(self) -> None:
        """Simulates hard worker stop: registry row stays in_progress with partial snapshots."""
        batch_id = _new_batch_id()
        try:
            with SessionLocal() as db:
                begin_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id=batch_id)
                _insert_bama_snapshot(db, batch_id=batch_id, bama_car_price_id=900_000_002)
                db.commit()
                row = db.get(ZeroPriceScrapeBatch, batch_id)
                self.assertEqual(row.status, STATUS_IN_PROGRESS)
            with SessionLocal() as db:
                latest_completed = db.execute(
                    text(
                        """
                        SELECT batch_id FROM zero_price_scrape_batch
                        WHERE source = 'bama' AND status = 'completed' AND batch_id = :bid
                        """
                    ),
                    {"bid": batch_id},
                ).first()
                self.assertIsNone(latest_completed)
        finally:
            self._cleanup_batch(batch_id)


if __name__ == "__main__":
    unittest.main()
