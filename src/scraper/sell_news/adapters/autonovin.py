"""Autonovin official sale circulars via public CarsAndBrandService API."""

from __future__ import annotations

import html
import io
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin

from requests.exceptions import RequestException, Timeout

from src.scraper.sell_news.adapters.base import AdapterStats, SourceAdapter
from src.scraper.sell_news.build import assemble_article
from src.scraper.sell_news.constants import DAILY_LIST_PAGES, DAILY_LOOKBACK_DAYS, SOURCE_AUTONOVIN
from src.scraper.sell_news.normalize import lookback_cutoff, parse_jalali_datetime, tehran_now
from src.scraper.sell_news.records import NewsArticle

LOGGER = logging.getLogger(__name__)

LIST_URL = "https://autonovin.ir/announcement"
API_URL = (
    "https://order.autonovin.ir/api/services/app/"
    "CarsAndBrandService/GetProductAndSaleDetailListV2"
)
PUBLIC_FILE_BASE = "https://autonovin.ir/public/"
PAGE_SIZE = 100
SALE_PROCESSES = (1, 3, 5)
CIRCULAR_TYPE_TITLE = "بخشنامه"


class AutonovinAdapter(SourceAdapter):
    source = SOURCE_AUTONOVIN
    list_url = LIST_URL

    def discover_and_fetch(
        self,
        *,
        backfill: bool = False,
        list_pages: int = DAILY_LIST_PAGES,
        lookback_days: int = DAILY_LOOKBACK_DAYS,
        limit: int | None = None,
    ) -> tuple[list[NewsArticle], AdapterStats]:
        del list_pages
        stats = AdapterStats(source=self.source)
        cutoff = lookback_cutoff(lookback_days)
        try:
            products = self._fetch_all_products()
        except Timeout:
            stats.timeout = True
            stats.error += 1
            stats.notes.append("api timeout")
            return [], stats
        except RequestException as exc:
            stats.error += 1
            stats.notes.append(f"api error: {type(exc).__name__}: {exc}")
            LOGGER.warning("autonovin list error %s", exc)
            return [], stats
        except Exception:
            stats.error += 1
            stats.notes.append("api error")
            LOGGER.exception("autonovin list failed")
            return [], stats

        details = _flatten_sale_details(products)
        horizon = tehran_now() + timedelta(days=2)
        selected: list[dict[str, Any]] = []
        for item in details:
            published = item.get("published_at")
            if backfill:
                selected.append(item)
                continue
            if published is None:
                selected.append(item)
                continue
            if cutoff <= published <= horizon:
                selected.append(item)
        if limit is not None:
            selected = selected[:limit]
        stats.found = len(selected)

        articles: list[NewsArticle] = []
        for item in selected:
            try:
                articles.append(self._detail_to_article(item))
                stats.fetched += 1
            except Exception:
                stats.error += 1
                LOGGER.exception("autonovin parse failed uid=%s", item.get("uid"))
            self.http.sleep_polite()
        return articles, stats

    def _fetch_all_products(self) -> list[dict[str, Any]]:
        by_id: dict[int, dict[str, Any]] = {}
        for process in SALE_PROCESSES:
            skip = 0
            total: int | None = None
            while True:
                payload = self._fetch_page(skip, process)
                result = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    break
                if total is None:
                    total = int(result.get("totalCount") or 0)
                items = result.get("items") or []
                if not isinstance(items, list) or not items:
                    break
                for product in items:
                    if not isinstance(product, dict):
                        continue
                    pid = product.get("id")
                    if isinstance(pid, int):
                        by_id[pid] = product
                    else:
                        by_id[len(by_id)] = product
                skip += len(items)
                if total is not None and skip >= total:
                    break
                if len(items) < PAGE_SIZE:
                    break
                self.http.sleep_polite()
        return list(by_id.values())

    def _fetch_page(self, skip: int, sale_process: int) -> dict[str, Any]:
        response = self.http.get(
            API_URL,
            params={
                "HasReturnCount": "true",
                "HasSaleDetail": "true",
                "HasProperty": "true",
                "SkipCount": skip,
                "MaxResultCount": PAGE_SIZE,
                "SaleProcess": sale_process,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _detail_to_article(self, item: dict[str, Any]) -> NewsArticle:
        pdf_url = item.get("pdf_url")
        pdf_text = self._pdf_text(str(pdf_url)) if pdf_url else None
        body_html = _synthetic_html(item, pdf_text=pdf_text)
        raw = {
            "uid": item.get("uid"),
            "announcement_id": item.get("announcement_id"),
            "sale_process": item.get("sale_process"),
            "sale_plan_code": item.get("sale_plan_code"),
            "product_id": item.get("product_id"),
            "organization": item.get("company"),
            "price_type": item.get("price_type"),
            "pdf_url": pdf_url,
            "sale_plan_start": item.get("start_raw"),
            "sale_plan_end": item.get("end_raw"),
        }
        return assemble_article(
            source=self.source,
            url=item["url"],
            title=item["title"],
            html=body_html,
            published_at=item.get("published_at"),
            source_list_url=self.list_url,
            source_article_id=str(item.get("uid") or item.get("announcement_id") or ""),
            http_status=200,
            raw_json=raw,
        )

    def _pdf_text(self, url: str) -> str | None:
        if not url.lower().endswith(".pdf"):
            return None
        try:
            response = self.http.get(url)
            if response.status_code >= 400:
                return None
            import pdfplumber

            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                chunks: list[str] = []
                for page in pdf.pages[:8]:
                    text = page.extract_text() or ""
                    if text.strip():
                        chunks.append(text.strip())
                joined = "\n\n".join(chunks).strip()
                return joined or None
        except Exception as exc:
            LOGGER.info("autonovin pdf skip %s %s", url, type(exc).__name__)
            return None


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _flatten_sale_details(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for product in products:
        org = product.get("organization") if isinstance(product.get("organization"), dict) else {}
        company = str((org or {}).get("title") or "").strip()
        product_title = str(product.get("title") or "").strip()
        product_id = product.get("id")
        for sale in product.get("saleDetails") or []:
            if not isinstance(sale, dict):
                continue
            uid = str(sale.get("uid") or "").strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            start_raw = _first(sale, "salePlanStartDate", "salePlanStartDate")
            end_raw = _first(sale, "salePlanEndDate", "salePlanEndDate")
            price = _first(sale, "finalPrice", "finalPrice", "carFee", "carFee")
            deliver_days = _first(sale, "deliverDaysCount", "deliverDaysCount")
            delivery = f"{deliver_days} روز" if deliver_days not in (None, "", 0) else ""
            deliver_date = _first(sale, "carDeliverDate", "carDeliverDate")
            if not delivery and deliver_date:
                delivery = str(deliver_date)
            sale_title = str(sale.get("title") or "").strip()
            title = sale_title or " ".join(x for x in (company, product_title) if x)
            out.append(
                {
                    "uid": uid,
                    "url": f"https://autonovin.ir/announcement/{uid}",
                    "title": title,
                    "company": company,
                    "product_title": product_title,
                    "product_id": product_id,
                    "price": price,
                    "price_type": _first(sale, "priceType", "priceType"),
                    "plan": _first(sale, "esaleTypeName", "esaleTypeName") or "",
                    "delivery": delivery,
                    "sale_process": _first(sale, "saleProcess", "saleProcess"),
                    "sale_plan_code": _first(sale, "salePlanCode", "salePlanCode"),
                    "announcement_id": _first(sale, "announcementId", "announcementId"),
                    "pdf_url": _circular_pdf_url(sale),
                    "start_raw": start_raw,
                    "end_raw": end_raw,
                    "published_at": parse_jalali_datetime(str(start_raw) if start_raw else None),
                }
            )
    return out


def _circular_pdf_url(sale: dict[str, Any]) -> str | None:
    info = _first(sale, "specificAnnouncementInfo", "specificAnnouncementInfo")
    if not isinstance(info, dict):
        return None
    attachments = info.get("attachments") or []
    if not isinstance(attachments, list):
        return None
    for att in attachments:
        if not isinstance(att, dict):
            continue
        type_title = str(_first(att, "typeTitle", "typeTitle") or "")
        file_name = str(_first(att, "fileName", "fileName") or "")
        direct = att.get("url")
        if type_title != CIRCULAR_TYPE_TITLE:
            continue
        if isinstance(direct, str) and direct.startswith("http"):
            return direct
        if file_name:
            return urljoin(PUBLIC_FILE_BASE, file_name)
    return None


def _synthetic_html(item: dict[str, Any], *, pdf_text: str | None) -> str:
    price = item.get("price")
    try:
        price_cell = f"{int(float(price)):,}" if price not in (None, "") else ""
    except (TypeError, ValueError):
        price_cell = str(price or "")
    start = item.get("start_raw") or ""
    end = item.get("end_raw") or ""
    window = " تا ".join(x for x in (str(start)[:19], str(end)[:19]) if x)
    paragraphs = [f"<p>{html.escape(str(item.get('title') or ''))}</p>"]
    if item.get("company"):
        paragraphs.append(f"<p>شرکت عرضه‌کننده: {html.escape(str(item['company']))}</p>")
    if window:
        paragraphs.append(f"<p>بازه طرح: {html.escape(window)}</p>")
    table = (
        "<table><thead><tr>"
        "<th>شرکت</th><th>نام خودرو</th><th>قیمت قطعی (ریال)</th>"
        "<th>موعد تحویل</th><th>نوع فروش</th>"
        "</tr></thead><tbody><tr>"
        f"<td>{html.escape(str(item.get('company') or ''))}</td>"
        f"<td>{html.escape(str(item.get('product_title') or ''))}</td>"
        f"<td>{html.escape(price_cell)}</td>"
        f"<td>{html.escape(str(item.get('delivery') or ''))}</td>"
        f"<td>{html.escape(str(item.get('plan') or ''))}</td>"
        "</tr></tbody></table>"
    )
    if pdf_text:
        paragraphs.append("<p>متن بخشنامه:</p>")
        for chunk in pdf_text.split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                paragraphs.append(f"<p>{html.escape(chunk)}</p>")
    body = "\n".join(paragraphs) + table
    return f'<div class="announcement">{body}</div>'
