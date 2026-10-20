"""Unit tests for zero-price batch completion registry."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.scraper.zero_price_batch_registry import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    ZeroPriceBatchInvalidCompletionError,
    ZeroPriceBatchSource,
    abort_zero_price_batch,
    begin_zero_price_batch,
    complete_zero_price_batch,
)


class ZeroPriceBatchRegistryTests(unittest.TestCase):
    def test_begin_inserts_in_progress(self) -> None:
        db = MagicMock()
        begin_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id="20260101T000000Z-abc")
        assert db.execute.called

    def test_complete_refuses_zero_row_count_without_db(self) -> None:
        db = MagicMock()
        with self.assertRaises(ZeroPriceBatchInvalidCompletionError):
            complete_zero_price_batch(
                db,
                source=ZeroPriceBatchSource.IRANJIB,
                batch_id="20260101T000000Z-abc",
                raw_row_count=0,
            )

    def test_abort_deletes_snapshots_and_marks_failed(self) -> None:
        db = MagicMock()
        deleted = MagicMock()
        deleted.rowcount = 3
        db.execute.return_value = deleted
        abort_zero_price_batch(db, source=ZeroPriceBatchSource.BAMA, batch_id="20260101T000000Z-abc")
        self.assertGreaterEqual(db.execute.call_count, 2)

    def test_status_constants(self) -> None:
        self.assertEqual(STATUS_IN_PROGRESS, "in_progress")
        self.assertEqual(STATUS_COMPLETED, "completed")
        self.assertEqual(STATUS_FAILED, "failed")


class BamaPipelineAbortOnEmptyTests(unittest.TestCase):
    def test_empty_run_calls_abort(self) -> None:
        from src.scraper.bama_price_service import BamaHierarchyTransientError, run_bama_zero_price_pipeline

        with (
            patch(
                "src.scraper.bama_price_service.iterate_hierarchy_items",
                return_value=iter(()),
            ),
            patch("src.scraper.bama_price_service.SessionLocal") as session_local,
            patch("src.scraper.bama_price_service.abort_zero_price_batch") as abort_mock,
            patch("src.scraper.bama_price_service.begin_zero_price_batch"),
            patch("src.scraper.bama_price_service.complete_zero_price_batch") as complete_mock,
        ):
            session_local.return_value.__enter__.return_value = MagicMock()
            with self.assertRaises(BamaHierarchyTransientError):
                run_bama_zero_price_pipeline(page_size=500, dry_run=False, capture_raw_rows=True)
            abort_mock.assert_called()
            complete_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
