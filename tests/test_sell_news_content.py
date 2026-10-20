"""Unit tests for structured sell-news article body (content_blocks)."""

from __future__ import annotations

import unittest

from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.content import extract_structured_body, subtitle_for_article
from src.scraper.sell_news.parse import extract_cars_from_html


MID_TABLE_HTML = """
<div class="article-content">
  <h1>شرایط فروش کیا اسپورتیج</h1>
  <p>شرکت دینا موتور طرح فروش فوری را آغاز کرد.</p>
  <h2>شرایط فروش اقساطی</h2>
  <p>قیمت قطعی مدل ۱۴۰۴ به شرح جدول است.</p>
  <table>
    <tr><th>خودرو</th><th>قیمت (تومان)</th><th>زمان تحویل</th></tr>
    <tr><td>کیا اسپورتیج ۲۰۲۵</td><td>۸،۷۶۲،۲۰۰،۰۰۰</td><td>۲۰ روز</td></tr>
    <tr><td>کیا اسپورتیج پلاس</td><td>۹،۰۰۰،۰۰۰،۰۰۰</td><td>۳۰ روز</td></tr>
  </table>
  <p>ثبت‌نام از طریق سایت شرکت انجام می‌شود.</p>
  <img src="/logo.png" alt="لوگو">
  <div class="related-news">اخبار مرتبط سایپا</div>
  <p>موضوعات داغ ثبت نام ایران خودرو ورود | ثبت نام</p>
</div>
"""


NOISE_PAGE_HTML = """
<html><body>
  <header>منوی سایت</header>
  <article class="post-content">
    <p>فقط همین پاراگراف خبر است.</p>
  </article>
  <footer>ورود | ثبت نام</footer>
</body></html>
"""


class ContentBlockTests(unittest.TestCase):
    def test_cars_block_stays_between_paragraphs(self) -> None:
        cars, method = extract_cars_from_html(MID_TABLE_HTML, title="شرایط فروش کیا اسپورتیج")
        self.assertEqual(method, "html_table")
        self.assertEqual(len(cars), 2)
        body = extract_structured_body(
            MID_TABLE_HTML,
            title="شرایط فروش کیا اسپورتیج",
            source="khodro45",
            cars=cars,
        )
        types = [b["type"] for b in body.blocks]
        self.assertIn("cars", types)
        self.assertGreaterEqual(len(body.blocks), 2)
        cars_at = types.index("cars")
        self.assertGreater(cars_at, 0)
        self.assertLess(cars_at, len(types) - 1)
        self.assertEqual(types[cars_at - 1], "paragraph")
        self.assertEqual(types[cars_at + 1], "paragraph")
        self.assertEqual(body.blocks[cars_at]["row_indexes"], [0, 1])
        self.assertNotIn("image", types)
        joined = " ".join(str(b) for b in body.blocks)
        self.assertNotIn("موضوعات داغ", joined)
        self.assertNotIn("ورود | ثبت نام", joined)
        self.assertNotIn("اخبار مرتبط", joined)
        self.assertIsNotNone(body.full_text)
        assert body.full_text is not None
        self.assertIn("\n\n", body.full_text)
        self.assertIn("شرایط فروش اقساطی", body.full_text)
        self.assertNotIn("۸،۷۶۲،۲۰۰،۰۰۰", body.full_text)
        self.assertIn("۱۴۰۴", body.full_text)

    def test_heading_skips_page_title(self) -> None:
        body = extract_structured_body(
            MID_TABLE_HTML,
            title="شرایط فروش کیا اسپورتیج",
            source="khodro45",
        )
        headings = [b for b in body.blocks if b["type"] == "heading"]
        self.assertTrue(headings)
        self.assertEqual(headings[0]["level"], 2)
        self.assertEqual(headings[0]["text"], "شرایط فروش اقساطی")
        self.assertFalse(any(h["text"] == "شرایط فروش کیا اسپورتیج" for h in headings))

    def test_persian_digits_not_converted(self) -> None:
        html = '<div class="post-content"><p>مهلت تا ۲۴ مرداد ۱۴۰۵ است.</p></div>'
        body = extract_structured_body(html, title="خبر", source="khodro45")
        self.assertEqual(body.blocks[0]["text"], "مهلت تا ۲۴ مرداد ۱۴۰۵ است.")
        self.assertIn("۲۴", body.full_text or "")
        self.assertNotIn("24", body.full_text or "")

    def test_list_and_quote(self) -> None:
        html = """
        <div class="entry-content">
          <p>مدارک لازم:</p>
          <ul><li>کارت ملی</li><li>شناسنامه</li></ul>
          <blockquote>ثبت‌نام فقط به‌صورت اینترنتی است</blockquote>
        </div>
        """
        body = extract_structured_body(html, title="مدارک", source="khodro45")
        types = [b["type"] for b in body.blocks]
        self.assertEqual(types, ["paragraph", "list", "quote"])
        self.assertFalse(body.blocks[1]["ordered"])
        self.assertEqual(body.blocks[1]["items"], ["کارت ملی", "شناسنامه"])
        self.assertIn("• کارت ملی", body.full_text or "")
        self.assertIn("• شناسنامه", body.full_text or "")

    def test_two_tables_row_indexes(self) -> None:
        html = """
        <div class="news-single_content">
          <p>جدول اول</p>
          <table>
            <tr><th>خودرو</th><th>قیمت (تومان)</th><th>زمان تحویل</th></tr>
            <tr><td>کیا اسپورتیج</td><td>۱۰۰۰۰۰۰</td><td>۱۰ روز</td></tr>
          </table>
          <p>جدول دوم</p>
          <table>
            <tr><th>خودرو</th><th>قیمت (تومان)</th><th>زمان تحویل</th></tr>
            <tr><td>هیوندای توسان</td><td>۲۰۰۰۰۰۰</td><td>۲۰ روز</td></tr>
            <tr><td>هیوندای سانتافه</td><td>۳۰۰۰۰۰۰</td><td>۳۰ روز</td></tr>
          </table>
          <p>پایان</p>
        </div>
        """
        cars, _method = extract_cars_from_html(html, title="دو جدول")
        body = extract_structured_body(html, title="دو جدول", source="khodrobank", cars=cars)
        car_blocks = [b for b in body.blocks if b["type"] == "cars"]
        self.assertEqual(len(car_blocks), 2)
        self.assertEqual(car_blocks[0]["row_indexes"], [0])
        self.assertEqual(car_blocks[1]["row_indexes"], [1, 2])
        self.assertEqual([c.row_index for c in cars], [0, 1, 2])

    def test_cars_only_parse_ok(self) -> None:
        html = """
        <table>
          <tr><th>خودرو</th><th>قیمت (تومان)</th><th>زمان تحویل</th></tr>
          <tr><td>کیا اسپورتیج</td><td>۸۰۰۰۰۰۰</td><td>۲۰ روز</td></tr>
        </table>
        """
        article = assemble_article(
            source="car.ir",
            url="https://car.ir/news/1-test",
            title="جدول فروش",
            html=html,
            published_at=None,
            source_list_url="https://car.ir/news/sell",
        )
        self.assertTrue(article.parse_ok)
        self.assertEqual(article.content_blocks, [{"type": "cars", "row_indexes": [0]}])
        self.assertIsNone(article.full_text)

    def test_subtitle_not_copy_of_body(self) -> None:
        html = '<div class="post-content"><p>یک جمله کوتاه.</p></div>'
        full = extract_structured_body(html, title="ت", source="khodro45").full_text
        self.assertIsNone(subtitle_for_article(full, full))
        self.assertEqual(
            subtitle_for_article("چکیده واقعی خبر.", full),
            "چکیده واقعی خبر.",
        )

    def test_nav_noise_stripped_from_page(self) -> None:
        body = extract_structured_body(NOISE_PAGE_HTML, title="خبر", source="khodro45")
        self.assertEqual([b["type"] for b in body.blocks], ["paragraph"])
        self.assertEqual(body.blocks[0]["text"], "فقط همین پاراگراف خبر است.")
        self.assertNotIn("ورود", body.full_text or "")

    def test_full_page_chrome_not_used(self) -> None:
        html = """
        <html><body>
          <!-- Google Tag Manager (noscript) -->
          <p>جستجوهای محبوب</p>
          <article>
            <p>شرایط فروش در منوی سایت</p>
          </article>
        </body></html>
        """
        fragment = '<div class="entry-content"><p>فقط متن مقاله.</p><p>پاراگراف دوم خبر.</p></div>'
        from src.scraper.sell_news.content import is_usable_article_html

        self.assertFalse(is_usable_article_html(html))
        self.assertTrue(is_usable_article_html(fragment))
        body = extract_structured_body(fragment, title="خبر", source="khodro45")
        self.assertEqual([b["type"] for b in body.blocks], ["paragraph", "paragraph"])
        self.assertNotIn("جستجو", body.full_text or "")


if __name__ == "__main__":
    unittest.main()
