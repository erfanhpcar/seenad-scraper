"""Unit tests for IranJib HTML price parser (iid, sentinels, digits, expand rows)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from src.scraper.iranjib_price_service import (
    IranJibParseError,
    IranJibTransientError,
    classify_market_status,
    dump_failed_html,
    fetch_price_page,
    parse_change_fields,
    parse_price_html,
    parse_toman_amount,
    run_iranjib_zero_price_pipeline,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "iranjib_price_sample.html"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class NormalizeTests(unittest.TestCase):
    def test_persian_digits_to_toman(self) -> None:
        self.assertEqual(parse_toman_amount("۱,۶۲۰,۰۰۰,۰۰۰"), 1_620_000_000)

    def test_sentinel_not_numeric(self) -> None:
        self.assertIsNone(parse_toman_amount("ناموجود"))
        self.assertIsNone(parse_toman_amount("به زودی"))
        self.assertIsNone(parse_toman_amount("توقف فروش"))
        self.assertIsNone(parse_toman_amount("---"))

    def test_status_map(self) -> None:
        self.assertEqual(classify_market_status("۱,۶۲۰,۰۰۰,۰۰۰"), "AVAILABLE")
        self.assertEqual(classify_market_status("ناموجود"), "UNAVAILABLE")
        self.assertEqual(classify_market_status("توقف فروش"), "STOPPED")
        self.assertEqual(classify_market_status("به زودی"), "COMING_SOON")
        self.assertEqual(classify_market_status("---"), "UNKNOWN")

    def test_change_split_with_bidi_marks(self) -> None:
        percent, amount = parse_change_fields("(\u200e-0.20%\u200f)\u200e-5,000,000\u200f")
        self.assertEqual(percent, "-0.20%")
        self.assertEqual(amount, "-5,000,000")


class TableParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = parse_price_html(
            _html(),
            page_kind="domestic",
            page_url="https://www.iranjib.ir/showgroup/45/x/",
        )

    def test_skips_expand_header_and_disclaimer(self) -> None:
        titles = [row.title_raw for row in self.rows]
        self.assertIn("وانت آریسان", titles)
        self.assertNotIn("نام خودرو", titles)
        self.assertTrue(all("نمايشگاه" not in row.title_raw for row in self.rows))
        self.assertEqual(len(self.rows), 7)

    def test_extracts_iid_from_chart_link(self) -> None:
        arisan = next(row for row in self.rows if row.title_raw == "وانت آریسان")
        self.assertEqual(arisan.iranjib_iid, 16025)
        self.assertEqual(arisan.section_raw, "ایران خودرو")
        self.assertEqual(arisan.market_price_toman, 1_620_000_000)
        self.assertEqual(arisan.factory_price_toman, 1_571_900_000)
        self.assertEqual(arisan.status, "AVAILABLE")

    def test_negative_change(self) -> None:
        soren = next(row for row in self.rows if "سورن" in row.title_raw)
        self.assertEqual(soren.iranjib_iid, 19348)
        self.assertEqual(soren.change_percent_raw, "-0.20%")
        self.assertEqual(soren.change_amount_raw, "-5,000,000")

    def test_unavailable_and_stopped_factory(self) -> None:
        hayma = next(row for row in self.rows if row.title_raw == "هایما 7X")
        self.assertEqual(hayma.status, "UNAVAILABLE")
        self.assertIsNone(hayma.market_price_toman)
        self.assertEqual(hayma.factory_price_raw, "توقف تولید")
        self.assertIsNone(hayma.factory_price_toman)

        avatar = next(row for row in self.rows if row.title_raw == "اواتار 11")
        self.assertEqual(avatar.status, "UNAVAILABLE")
        self.assertEqual(avatar.factory_price_raw, "توقف فروش")

    def test_coming_soon_factory_keeps_market_price(self) -> None:
        sahand = next(row for row in self.rows if "سهند" in row.title_raw)
        self.assertEqual(sahand.section_raw, "سایپا")
        self.assertEqual(sahand.status, "AVAILABLE")
        self.assertEqual(sahand.market_price_toman, 1_977_000_000)
        self.assertEqual(sahand.factory_price_raw, "به زودی")
        self.assertIsNone(sahand.factory_price_toman)

    def test_missing_iid_fallback_and_unknown_price(self) -> None:
        orphan = next(row for row in self.rows if row.title_raw == "خودرو بدون نمودار")
        self.assertIsNone(orphan.iranjib_iid)
        self.assertEqual(orphan.status, "UNKNOWN")
        self.assertIsNone(orphan.market_price_toman)

    def test_second_table_imported_brand(self) -> None:
        s90 = next(row for row in self.rows if row.title_raw == "S90")
        self.assertEqual(s90.section_raw, "ولوو - Volvo")
        self.assertEqual(s90.iranjib_iid, 19350)
        self.assertEqual(s90.market_price_toman, 28_500_000_000)


def _html_response(status: int, body: str = "<html></html>", *, url: str = "https://www.iranjib.ir/x/") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.reason = "Gateway Timeout" if status == 504 else "OK"
    response.url = url
    response.headers = {}
    response.text = body
    response.encoding = "utf-8"
    if status >= 400:
        error = requests.HTTPError(f"{status} for url: {url}", response=response)
        response.raise_for_status.side_effect = error
    else:
        response.raise_for_status.return_value = None
    return response


class FetchRetryTests(unittest.TestCase):
    def test_retries_read_timeout_then_succeeds(self) -> None:
        session = MagicMock()
        ok = _html_response(200, "<html><table class='items_table'></table></html>")
        session.get.side_effect = [
            requests.ReadTimeout("read timed out"),
            requests.ReadTimeout("read timed out"),
            ok,
        ]
        with patch("src.scraper.iranjib_price_service.time.sleep"):
            html = fetch_price_page(
                session,
                url="https://www.iranjib.ir/x/",
                max_attempts=5,
                backoff_base=0.01,
                backoff_cap=0.02,
            )
        self.assertIn("items_table", html)
        self.assertEqual(session.get.call_count, 3)

    def test_retries_http_503_then_succeeds(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _html_response(503),
            _html_response(200, "<html><table class='items_table'></table></html>"),
        ]
        with patch("src.scraper.iranjib_price_service.time.sleep"):
            html = fetch_price_page(
                session,
                url="https://www.iranjib.ir/x/",
                max_attempts=4,
                backoff_base=0.01,
                backoff_cap=0.02,
            )
        self.assertIn("items_table", html)
        self.assertEqual(session.get.call_count, 2)

    def test_does_not_retry_http_403(self) -> None:
        session = MagicMock()
        session.get.return_value = _html_response(403)
        with self.assertRaises(requests.HTTPError) as ctx:
            fetch_price_page(session, url="https://www.iranjib.ir/x/", max_attempts=5)
        self.assertEqual(ctx.exception.response.status_code, 403)
        self.assertEqual(session.get.call_count, 1)

    def test_exhausts_timeouts_and_raises(self) -> None:
        session = MagicMock()
        session.get.side_effect = requests.ReadTimeout("read timed out")
        with patch("src.scraper.iranjib_price_service.time.sleep"):
            with self.assertRaises(requests.ReadTimeout):
                fetch_price_page(
                    session,
                    url="https://www.iranjib.ir/x/",
                    max_attempts=3,
                    backoff_base=0.01,
                    backoff_cap=0.02,
                )
        self.assertEqual(session.get.call_count, 3)

    def test_retries_anti_bot_then_succeeds(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            _html_response(200, "<html>Just a moment</html>"),
            _html_response(200, "<html><table class='items_table'></table></html>"),
        ]
        with patch("src.scraper.iranjib_price_service.time.sleep"):
            html = fetch_price_page(
                session,
                url="https://www.iranjib.ir/x/",
                max_attempts=4,
                backoff_base=0.01,
                backoff_cap=0.02,
            )
        self.assertIn("items_table", html)
        self.assertEqual(session.get.call_count, 2)


class EmptySnapshotRetryTests(unittest.TestCase):
    def test_empty_parse_raises_transient(self) -> None:
        empty_html = "<html><table class='items_table'><tr class='header'><th>نام خودرو</th></tr></table></html>"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"IRANJIB_FAILED_HTML_DIR": tmp}):
                with patch(
                    "src.scraper.iranjib_price_service.fetch_price_page",
                    return_value=empty_html,
                ):
                    with self.assertRaises(IranJibTransientError):
                        run_iranjib_zero_price_pipeline(dry_run=False, page_kinds=("domestic",))

    def test_empty_parse_dumps_html(self) -> None:
        empty_html = "<html><table class='items_table'><tr class='header'><th>نام خودرو</th></tr></table></html>"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"IRANJIB_FAILED_HTML_DIR": tmp}):
                with patch(
                    "src.scraper.iranjib_price_service.fetch_price_page",
                    return_value=empty_html,
                ):
                    with self.assertRaises(IranJibTransientError):
                        run_iranjib_zero_price_pipeline(dry_run=True, page_kinds=("domestic",))
            latest = Path(tmp) / "iranjib_fail_domestic_latest.html"
            self.assertTrue(latest.is_file())
            dumped = latest.read_text(encoding="utf-8")
            self.assertIn("reason=empty_parse", dumped)
            self.assertIn("items_table", dumped)
            stamped = list(Path(tmp).glob("iranjib_fail_domestic_20*.html"))
            self.assertEqual(len(stamped), 1)

    def test_no_table_dumps_html(self) -> None:
        html = "<html><body>Just a catalogue shell</body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"IRANJIB_FAILED_HTML_DIR": tmp}):
                with patch(
                    "src.scraper.iranjib_price_service.fetch_price_page",
                    return_value=html,
                ):
                    with self.assertRaises(IranJibParseError):
                        run_iranjib_zero_price_pipeline(dry_run=True, page_kinds=("domestic",))
            latest = Path(tmp) / "iranjib_fail_domestic_latest.html"
            self.assertTrue(latest.is_file())
            self.assertIn("reason=no_table", latest.read_text(encoding="utf-8"))


class FailedHtmlDumpTests(unittest.TestCase):
    def test_dump_writes_latest_and_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"IRANJIB_FAILED_HTML_DIR": tmp}):
                path = dump_failed_html(
                    "<html>payload</html>",
                    page_kind="domestic",
                    url="https://www.iranjib.ir/x/",
                    reason="empty_parse",
                )
            self.assertIsNotNone(path)
            text = Path(tmp, "iranjib_fail_domestic_latest.html").read_text(encoding="utf-8")
            self.assertIn("reason=empty_parse", text)
            self.assertIn("<html>payload</html>", text)


class CeleryRetryConfigTests(unittest.TestCase):
    def test_iranjib_backoff_is_sixty_seconds_without_jitter(self) -> None:
        from app.tasks.iranjib_zero_price import iranjib_zero_price_scrape

        self.assertEqual(iranjib_zero_price_scrape.retry_backoff, 60)
        self.assertFalse(iranjib_zero_price_scrape.retry_jitter)
        self.assertEqual(iranjib_zero_price_scrape.retry_kwargs.get("max_retries"), 3)
        self.assertNotIn("countdown", iranjib_zero_price_scrape.retry_kwargs)

    def test_bama_backoff_is_sixty_seconds_without_jitter(self) -> None:
        from app.tasks.bama_zero_price import bama_zero_price_scrape

        self.assertEqual(bama_zero_price_scrape.retry_backoff, 60)
        self.assertFalse(bama_zero_price_scrape.retry_jitter)
        self.assertEqual(bama_zero_price_scrape.retry_kwargs.get("max_retries"), 3)
        self.assertNotIn("countdown", bama_zero_price_scrape.retry_kwargs)


if __name__ == "__main__":
    unittest.main()
