"""Unit tests for Bama hierarchy HTTP retry (timeouts / gateway flaps)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from src.scraper.bama_price_service import (
    BamaHierarchyTransientError,
    fetch_hierarchy_page,
    iterate_hierarchy_items,
    run_bama_zero_price_pipeline,
)


def _json_response(status: int, payload: dict, *, url: str = "https://bama.ir/cad/api/price/hierarchy") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.reason = "Gateway Timeout" if status == 504 else "OK"
    response.url = url
    response.headers = {}
    response.json.return_value = payload
    if status >= 400:
        error = requests.HTTPError(f"{status} for url: {url}", response=response)
        response.raise_for_status.side_effect = error
    else:
        response.raise_for_status.return_value = None
    return response


class FetchHierarchyPageRetryTests(unittest.TestCase):
    def test_retries_read_timeout_then_succeeds(self) -> None:
        session = MagicMock()
        ok = _json_response(200, {"data": [{"items": [{"brand": "saipa", "model": "quick"}]}]})
        session.get.side_effect = [
            requests.ReadTimeout("read timed out"),
            requests.ReadTimeout("read timed out"),
            ok,
        ]

        with patch("src.scraper.bama_price_service.time.sleep"):
            blocks = fetch_hierarchy_page(
                session,
                url="https://bama.ir/cad/api/price/hierarchy",
                page_index=0,
                page_size=500,
                max_attempts=5,
                backoff_base=0.01,
                backoff_cap=0.02,
            )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(session.get.call_count, 3)

    def test_retries_http_503_then_succeeds(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _json_response(503, {}),
            _json_response(200, {"data": []}),
        ]

        with patch("src.scraper.bama_price_service.time.sleep"):
            blocks = fetch_hierarchy_page(
                session,
                url="https://bama.ir/cad/api/price/hierarchy",
                page_index=0,
                page_size=500,
                max_attempts=4,
                backoff_base=0.01,
                backoff_cap=0.02,
            )

        self.assertEqual(blocks, [])
        self.assertEqual(session.get.call_count, 2)

    def test_does_not_retry_http_403(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response(403, {})

        with self.assertRaises(requests.HTTPError) as ctx:
            fetch_hierarchy_page(
                session,
                url="https://bama.ir/cad/api/price/hierarchy",
                page_index=0,
                page_size=500,
                max_attempts=5,
            )

        self.assertEqual(ctx.exception.response.status_code, 403)
        self.assertEqual(session.get.call_count, 1)

    def test_exhausts_timeouts_and_raises(self) -> None:
        session = MagicMock()
        session.get.side_effect = requests.ReadTimeout("read timed out")

        with patch("src.scraper.bama_price_service.time.sleep"):
            with self.assertRaises(requests.ReadTimeout):
                fetch_hierarchy_page(
                    session,
                    url="https://bama.ir/cad/api/price/hierarchy",
                    page_index=0,
                    page_size=500,
                    max_attempts=3,
                    backoff_base=0.01,
                    backoff_cap=0.02,
                )

        self.assertEqual(session.get.call_count, 3)

    def test_iterate_uses_retried_page(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            requests.ReadTimeout("read timed out"),
            _json_response(
                200,
                {
                    "data": [
                        {
                            "brand": "saipa",
                            "brand_fa": "سایپا",
                            "items": [
                                {
                                    "brand": "saipa",
                                    "brand_fa": "سایپا",
                                    "model": "quick",
                                    "model_fa": "کوییک",
                                    "trim": "manual",
                                    "trim_fa": "دنده‌ای",
                                    "model_year": 1403,
                                    "price": 500000000,
                                    "car_price_id": 1,
                                }
                            ],
                        }
                    ]
                },
            ),
        ]

        with patch("src.scraper.bama_price_service.time.sleep"):
            rows = list(
                iterate_hierarchy_items(
                    session,
                    page_size=500,
                    timeout_seconds=1.0,
                    inter_page_delay_seconds=(0.0, 0.0),
                    max_hierarchy_pages=1,
                )
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 0)
        self.assertEqual(rows[0][1].bama_car_price_id, 1)


class EmptySnapshotRetryTests(unittest.TestCase):
    def test_empty_live_run_raises_transient(self) -> None:
        with (
            patch(
                "src.scraper.bama_price_service.iterate_hierarchy_items",
                return_value=iter(()),
            ),
            patch("src.scraper.bama_price_service.SessionLocal") as session_local,
        ):
            session_local.return_value.__enter__.return_value = MagicMock()
            with self.assertRaises(BamaHierarchyTransientError):
                run_bama_zero_price_pipeline(page_size=500, dry_run=False, capture_raw_rows=True)


if __name__ == "__main__":
    unittest.main()
