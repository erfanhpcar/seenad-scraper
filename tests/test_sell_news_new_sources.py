"""Unit tests for autonovin/megfa sell-news adapters (no network)."""

from __future__ import annotations

import unittest

from src.scraper.sell_news.adapters.autonovin import _flatten_sale_details, _synthetic_html
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import SOURCE_AUTONOVIN, SOURCE_MEGFA
from src.scraper.sell_news.normalize import parse_jalali_datetime
from src.scraper.sell_news.parse import extract_cars_from_html


MEGFA_HTML = """
<div class="announcement">
  <h1>اطلاعیه عرضه خودروهای وارداتی – ۱ شهریور ۱۴۰۵</h1>
  <p>به اطلاع متقاضیان می‌رساند از ۱۴۰۵/۰۶/۰۱ طرح فروش آغاز می‌شود.</p>
  <table>
    <thead>
      <tr>
        <th>ردیف</th><th>شرکت</th><th>برند</th><th>نام خودرو</th>
        <th>مدل خودرو</th><th>قیمت قطعی (ریال)</th><th>موعد تحویل</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>1</td><td>امیر تجارت سپنتا (کهن)</td><td>Toyota</td>
        <td>Frontlander</td><td>2026</td><td>73,771,000,000</td><td>۶۰ روز</td>
      </tr>
      <tr>
        <td>2</td><td>کریمان موتور</td><td>Nissan</td>
        <td>Qashqai</td><td>2025</td><td>41,000,000,000</td><td>۹۰ روز</td>
      </tr>
    </tbody>
  </table>
</div>
"""


class MegfaFixtureTests(unittest.TestCase):
    def test_assemble_parses_table_rows(self) -> None:
        article = assemble_article(
            source=SOURCE_MEGFA,
            url="https://salecars.ir/announcement/282",
            title="اطلاعیه عرضه خودروهای وارداتی – ۱ شهریور ۱۴۰۵",
            html=MEGFA_HTML,
            published_at=parse_jalali_datetime("1405/06/01"),
            source_list_url="https://salecars.ir/announcements",
            source_article_id="282",
            http_status=200,
        )
        self.assertTrue(article.parse_ok)
        self.assertEqual(len(article.cars), 2)
        self.assertEqual(article.cars[0].brand_raw, "Toyota")
        self.assertEqual(article.cars[0].price_toman, 7_377_100_000)
        self.assertEqual(article.cars[1].brand_raw, "Nissan")


class AutonovinFixtureTests(unittest.TestCase):
    def test_flatten_and_synthetic_table(self) -> None:
        products = [
            {
                "id": 1620,
                "title": "نامی باکس برقی",
                "organization": {"id": 268, "title": "نامی خودرو"},
                "saleDetails": [
                    {
                        "title": "عرضه مرداد ۱۴۰۵ _ نامی باکس _ نامی خودرو",
                        "uid": "1cf376ac-151a-4991-8993-2037b58f8721",
                        "carFee": 39600000000,
                        "finalPrice": 39600000000.0,
                        "priceType": "قیمت قطعی",
                        "salePlanStartDate": "2026-08-10T10:00:00",
                        "salePlanEndDate": "2026-08-12T00:00:00",
                        "salePlanCode": 1405050104,
                        "saleProcess": 1,
                        "deliverDaysCount": 30,
                        "announcementId": 335,
                        "esaleTypeName": "طرح عادی",
                        "specificAnnouncementInfo": {
                            "attachments": [
                                {
                                    "fileName": "ee366f39-06ae-42cc-87e5-93bd19b293d5.pdf",
                                    "typeTitle": "بخشنامه",
                                    "url": None,
                                }
                            ]
                        },
                    }
                ],
            }
        ]
        items = _flatten_sale_details(products)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["uid"], "1cf376ac-151a-4991-8993-2037b58f8721")
        self.assertTrue(str(item["pdf_url"]).endswith(".pdf"))
        html_body = _synthetic_html(item, pdf_text=None)
        rows, method = extract_cars_from_html(html_body, title=item["title"])
        self.assertEqual(method, "html_table")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].price_toman, 3_960_000_000)
        self.assertEqual(rows[0].delivery_raw, "30 روز")
        article = assemble_article(
            source=SOURCE_AUTONOVIN,
            url=item["url"],
            title=item["title"],
            html=html_body,
            published_at=item["published_at"],
            source_list_url="https://autonovin.ir/announcement",
            source_article_id=item["uid"],
            http_status=200,
            raw_json={"uid": item["uid"]},
        )
        self.assertTrue(article.parse_ok)
        self.assertEqual(article.cars[0].price_toman, 3_960_000_000)


if __name__ == "__main__":
    unittest.main()
