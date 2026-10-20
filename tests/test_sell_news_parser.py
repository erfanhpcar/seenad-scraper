"""Unit tests for sell-news table parser, prices, dates, and URL canonicalization."""

from __future__ import annotations

import unittest
from datetime import datetime

from src.scraper.sell_news.normalize import (
    canonical_url,
    classify_sale_type,
    parse_jalali_datetime,
    parse_price,
    split_brand_model,
)
from src.scraper.sell_news.parse import extract_cars_from_html, map_header, repair_broken_table_html


class NormalizeTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_and_slash(self) -> None:
        url = "https://Khodro45.com/mag/kia-sportage-terms-of-sale/?utm_source=x#frag"
        self.assertEqual(
            canonical_url(url),
            "https://khodro45.com/mag/kia-sportage-terms-of-sale",
        )

    def test_jalali_named_month(self) -> None:
        dt = parse_jalali_datetime("۲۴ مرداد ۱۴۰۵ - 08:15")
        assert dt is not None
        self.assertEqual(dt.tzinfo.key if hasattr(dt.tzinfo, "key") else str(dt.tzinfo), "Asia/Tehran")
        self.assertEqual(dt.hour, 8)
        self.assertEqual(dt.minute, 15)

    def test_jalali_numeric(self) -> None:
        dt = parse_jalali_datetime("1405/5/24 - 08:15")
        assert dt is not None
        self.assertEqual(dt.month, 8)
        self.assertEqual(dt.day, 15)

    def test_price_rial_to_toman(self) -> None:
        toman, unit = parse_price("۸۶,۹۵۰,۰۰۰,۰۰۰ ریال", header_hint="قیمت قطعی (ریال)")
        self.assertEqual(unit, "rial")
        self.assertEqual(toman, 8_695_000_000)

    def test_price_toman_dots(self) -> None:
        toman, unit = parse_price("۱۲.۴۰۰.۰۰۰.۰۰۰ تومان")
        self.assertEqual(unit, "toman")
        self.assertEqual(toman, 12_400_000_000)

    def test_price_words(self) -> None:
        toman, unit = parse_price("۵ میلیارد و ۷۵۰ میلیون تومان")
        self.assertEqual(unit, "toman")
        self.assertEqual(toman, 5_750_000_000)

    def test_sale_type_title_priority(self) -> None:
        code, raw = classify_sale_type("آغاز فروش فوری کیا اسپورتیج", "طرح وارداتی با قرعه‌کشی")
        self.assertEqual(code, "fouri")
        self.assertEqual(raw, "فروش فوری")

    def test_brand_split(self) -> None:
        brand, model = split_brand_model("کیا اسپورتیج توربو")
        self.assertEqual(brand, "کیا")
        self.assertEqual(model, "اسپورتیج توربو")


class HeaderMapTests(unittest.TestCase):
    def test_synonyms(self) -> None:
        self.assertEqual(map_header("نوع خودرو"), "vehicle")
        self.assertEqual(map_header("قیمت قطعی (ریال)"), "price")
        self.assertEqual(map_header("موعد تحویل"), "delivery")
        self.assertEqual(map_header("رنگ قابل عرضه"), "color")
        self.assertEqual(map_header("مدل خودرو"), "model")


class TableParserTests(unittest.TestCase):
    def test_khodro45_simple_table(self) -> None:
        html = """
        <table>
          <tr><th>خودرو</th><th>قیمت (تومان)</th><th>زمان تحویل</th></tr>
          <tr><td>کیا اسپورتیج ۲۰۲۵</td><td>۸،۷۶۲،۲۰۰،۰۰۰</td><td>۲۰ روز</td></tr>
        </table>
        """
        rows, method = extract_cars_from_html(html, title="شرایط فروش کیا اسپورتیج")
        self.assertEqual(method, "html_table")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand_raw, "کیا")
        self.assertIn("اسپورتیج", rows[0].model_raw or "")
        self.assertEqual(rows[0].price_toman, 8_762_200_000)
        self.assertEqual(rows[0].price_unit_guess, "toman")
        self.assertEqual(rows[0].delivery_raw, "20 روز")

    def test_khodro45_key_value(self) -> None:
        html = """
        <table>
          <tr><td>نام خودرو</td><td>کیا اسپورتیج توربو</td></tr>
          <tr><td>مدل خودرو</td><td>۲۰۲۵</td></tr>
          <tr><td>مبلغ نهایی خودرو</td><td>۵,۸۷۱,۴۰۰,۰۰۰ تومان</td></tr>
          <tr><td>زمان تحویل</td><td>۶۰روزه</td></tr>
        </table>
        """
        rows, method = extract_cars_from_html(html)
        self.assertEqual(method, "key_value_table")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand_raw, "کیا")
        self.assertEqual(rows[0].extra_json.get("model_year"), 2025)
        self.assertEqual(rows[0].price_toman, 5_871_400_000)
        self.assertEqual(rows[0].delivery_raw, "60روزه")

    def test_khodrobank_wide_table(self) -> None:
        html = """
        <table>
          <tr>
            <th>نوع خودرو</th><th>مدل</th><th>رنگ قابل عرضه</th>
            <th>قیمت قطعی (ریال)</th><th>جریمه تاخیر</th><th>پیش پرداخت</th><th>زمان تحویل</th>
          </tr>
          <tr>
            <td>کیا اسپورتیج</td><td>۲۰۲۵</td><td>سفید - خاکستری</td>
            <td>۸۶,۹۵۰,۰۰۰,۰۰۰ ریال</td><td>۳٪</td><td>۱۰ درصد</td><td>۷۲ ساعت</td>
          </tr>
        </table>
        """
        rows, method = extract_cars_from_html(html)
        self.assertEqual(method, "html_table")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand_raw, "کیا")
        self.assertEqual(rows[0].colors_raw, "سفید - خاکستری")
        self.assertEqual(rows[0].price_toman, 8_695_000_000)
        self.assertEqual(rows[0].delivery_raw, "72 ساعت")
        self.assertIn("پیش پرداخت", rows[0].extra_json)

    def test_khodrobank_broken_p_tags(self) -> None:
        raw = (
            '<table style="x"></p><p><thead></p><p><tr></p><p>'
            "<th>نام خودرو</th></p><p><th>قیمت قطعی</th></p><p>"
            "<th>موعد تحویل</th></p><p></tr></p><p></thead></p><p>"
            "<tbody></p><p><tr></p><p><td>فولکس واگن تیگوان L پرو</td></p><p>"
            "<td>۱۲.۴۰۰.۰۰۰.۰۰۰ تومان</td></p><p><td>حداکثر ۲۴ ساعت</td>"
            "</p><p></tr></p><p></tbody></p><p></table>"
        )
        repaired = repair_broken_table_html(raw)
        self.assertNotIn("<p>", repaired.lower())
        rows, method = extract_cars_from_html(raw)
        self.assertTrue(rows)
        self.assertEqual(rows[0].brand_raw, "فولکس واگن")
        self.assertEqual(rows[0].price_toman, 12_400_000_000)
        self.assertEqual(rows[0].delivery_raw, "حداکثر 24 ساعت")
        self.assertIn(method, ("html_table", "key_value_table"))

    def test_skip_organization_table(self) -> None:
        html = """
        <table>
          <tr><td colspan="2">اسامی سازمان‌های مجاز برای شرکت در طرح فروش</td></tr>
          <tr><td>اتاق اصناف تهران</td><td>شرکت پالایش گاز فجر جم</td></tr>
        </table>
        """
        rows, method = extract_cars_from_html(html, title="فروش سازمانی مدیران خودرو")
        self.assertEqual(rows, [])
        self.assertEqual(method, "")

    def test_car_ir_rial_headers(self) -> None:
        html = """
        <table>
          <tr>
            <th>نام خودرو</th><th>مدل</th><th>رنگ</th><th>تریم</th>
            <th>قیمت قطعی (ريال)</th><th>موعد تحویل</th>
          </tr>
          <tr>
            <td>کیا اسپورتیج 1.5 لیتری توربو</td><td>2025</td><td>سفید</td>
            <td>کرم روشن</td><td>87,622,000,000</td>
            <td>20 روز پس از تکمیل وجه و تحویل مدارک</td>
          </tr>
        </table>
        """
        rows, _method = extract_cars_from_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].trim_raw, "کرم روشن")
        self.assertEqual(rows[0].price_toman, 8_762_200_000)
        self.assertEqual(rows[0].price_unit_guess, "rial")

    def test_prose_fallback(self) -> None:
        html = "<p>شرکت دینا موتور فروش ویژه کیا اسپورتیج را با قیمت ۸،۷۰۰،۰۰۰،۰۰۰ تومان و تحویل ۳ روزه آغاز کرد.</p>"
        rows, method = extract_cars_from_html(
            html, title="آغاز ثبت نام کیا اسپورتیج با تحویل ۳ روزه"
        )
        self.assertEqual(method, "prose_fallback")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand_raw, "کیا")

    def test_megfa_imported_table(self) -> None:
        html = """
        <div class="announcement">
          <h1>اطلاعیه عرضه خودروهای وارداتی – ۱ شهریور ۱۴۰۵</h1>
          <p>از روز یکشنبه مورخ ۱۴۰۵/۰۶/۰۱ ثبت‌نام آغاز می‌شود.</p>
          <table>
            <tr>
              <th>ردیف</th><th>شرکت</th><th>برند</th><th>نام خودرو</th>
              <th>مدل خودرو</th><th>قیمت قطعی (ریال)</th><th>موعد تحویل</th>
            </tr>
            <tr>
              <td>1</td><td>امیر تجارت سپنتا (کهن)</td><td>Toyota</td>
              <td>Frontlander</td><td>2026</td><td>73,771,000,000</td><td>۶۰ روز</td>
            </tr>
          </table>
        </div>
        """
        rows, method = extract_cars_from_html(html, title="اطلاعیه عرضه خودروهای وارداتی")
        self.assertEqual(method, "html_table")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand_raw, "Toyota")
        self.assertEqual(rows[0].model_raw, "Frontlander")
        self.assertEqual(rows[0].price_toman, 7_377_100_000)
        self.assertEqual(rows[0].price_unit_guess, "rial")
        self.assertEqual(rows[0].delivery_raw, "60 روز")
        self.assertEqual(map_header("قیمت قطعی (ریال)"), "price")


if __name__ == "__main__":
    unittest.main()
