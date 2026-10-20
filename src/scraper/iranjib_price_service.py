"""
IranJib zero-km market-price raw ingestion (HTML tables on iranjib.ir).

Pages fetched each run (SSR HTML, no JSON API):

- domestic: ``/showgroup/45/قیمت-خودرو-تولید-داخل/``
- imported: ``/showgroup/46/قیمت-خودرو-وارداتی/``

Persist every data row as string fields + JSON in ``iranjib_zero_price_scrape_snapshot``
under a fresh ``batch_id``. Entity matching belongs on the Main Server.

Live DOM (confirmed by probe): ``table.items_table`` with ``tr.catsection`` brand
groups, ``tr.header`` column titles (قیمت بازار/کارخانه or نمایندگی **تومان**),
data rows with ``td.entryrtl`` + ``span.lastprice``, chart ``getchart.php?iid=``,
and ``tr.downN.expand`` chart placeholders that are not data.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from sqlalchemy.orm import Session

from src.scraper.zero_price_batch_registry import (
    ZeroPriceBatchSource,
    abort_zero_price_batch,
    begin_zero_price_batch,
    complete_zero_price_batch,
)

LOGGER: Final = logging.getLogger(__name__)

IRANJIB_BASE_URL: Final[str] = "https://www.iranjib.ir"
DOMESTIC_PATH: Final[str] = "/showgroup/45/قیمت-خودرو-تولید-داخل/"
IMPORTED_PATH: Final[str] = "/showgroup/46/قیمت-خودرو-وارداتی/"
PAGE_SOURCES: Final[tuple[tuple[str, str], ...]] = (
    ("domestic", f"{IRANJIB_BASE_URL}{DOMESTIC_PATH}"),
    ("imported", f"{IRANJIB_BASE_URL}{IMPORTED_PATH}"),
)
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 45.0
DEFAULT_HTTP_MAX_ATTEMPTS: Final[int] = 5
DEFAULT_HTTP_BACKOFF_BASE_S: Final[float] = 2.0
DEFAULT_HTTP_BACKOFF_CAP_S: Final[float] = 30.0
RAW_SNAPSHOT_DB_FLUSH_INTERVAL: Final[int] = 400
DEFAULT_FAILED_HTML_DIR: Final[str] = "data"
FAILED_HTML_MAX_BYTES: Final[int] = 2_000_000
RETRYABLE_HTTP_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ANTI_BOT_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "attention required",
    "cloudflare",
    "arvan cloud",
    "access denied",
    "enable javascript",
)
ARABIC_VARIANTS: Final[dict[int, str]] = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS: Final[dict[int, str]] = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
BIDI_MARKS: Final[str] = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
IID_HREF_RE: Final[re.Pattern[str]] = re.compile(r"iid=(\d+)", re.IGNORECASE)
PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
INT_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+$")
EXPAND_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"\bdown\d+\b")
STATUS_BY_TEXT: Final[dict[str, str]] = {
    "ناموجود": "UNAVAILABLE",
    "توقف فروش": "STOPPED",
    "توقف تولید": "STOPPED",
    "به زودی": "COMING_SOON",
    "---": "UNKNOWN",
    "----": "UNKNOWN",
    "-": "UNKNOWN",
}
VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "STOPPED", "COMING_SOON", "UNKNOWN"}
)
SKIP_SECTION_HINTS: Final[tuple[str, ...]] = (
    "توضیحات",
    "توضيحات",
    "قیمت خودرو های وارداتی",
    "قیمت خودرو های تولید داخل",
)


class IranJibTransientError(Exception):
    """Timeouts, gateway flaps, anti-bot, or empty catalogue worth retrying at Celery level."""


class IranJibParseError(IranJibTransientError):
    """DOM did not yield a usable price table. Retryable like an empty Bama snapshot."""


def generate_scrape_batch_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class IranJibPriceRow:
    page_kind: str
    page_url: str
    section_raw: str
    title_raw: str
    iranjib_iid: int | None
    market_price_raw: str
    factory_price_raw: str
    change_amount_raw: str
    change_percent_raw: str
    status_raw: str
    status: str
    market_price_toman: int | None
    factory_price_toman: int | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IranJibZeroPriceRunSummary:
    pages_fetched: int
    items_seen: int
    items_with_price: int
    items_with_iid: int
    domestic_rows: int
    imported_rows: int
    errors_http: int
    raw_rows_persisted: int
    batch_id: str | None

    def as_celery_dict(self) -> dict[str, Any]:
        return {
            "pages_fetched": self.pages_fetched,
            "items_seen": self.items_seen,
            "items_with_price": self.items_with_price,
            "items_with_iid": self.items_with_iid,
            "domestic_rows": self.domestic_rows,
            "imported_rows": self.imported_rows,
            "errors_http": self.errors_http,
            "raw_rows_persisted": self.raw_rows_persisted,
            "batch_id": self.batch_id,
        }


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
        }
    )
    return session


def _backoff_seconds(attempt: int, *, base: float, cap: float) -> float:
    wait = min(cap, base * (2 ** (attempt - 1)))
    return wait + random.uniform(0.0, wait * 0.25)


def _retry_after_seconds(response: requests.Response, fallback: float) -> float:
    raw = response.headers.get("Retry-After")
    if not raw:
        return fallback
    try:
        return max(fallback, float(raw))
    except ValueError:
        return fallback


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\xa0", " ").replace("\u200c", " ")
    for mark in BIDI_MARKS:
        text = text.replace(mark, "")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    return WS_RE.sub(" ", text).strip()


def parse_toman_amount(value: str | None) -> int | None:
    """Parse a displayed toman integer. Returns None for sentinels or garbage."""

    text = clean_text(value)
    if not text or text in STATUS_BY_TEXT:
        return None
    compact = text.replace(",", "").replace("،", "").replace(" ", "")
    if INT_RE.fullmatch(compact):
        return int(compact)
    return None


def classify_market_status(market_raw: str) -> str:
    text = clean_text(market_raw)
    if not text:
        return "UNKNOWN"
    mapped = STATUS_BY_TEXT.get(text)
    if mapped:
        return mapped
    if parse_toman_amount(text) is not None:
        return "AVAILABLE"
    return "UNKNOWN"


def parse_change_fields(change_raw: str) -> tuple[str, str]:
    text = clean_text(change_raw)
    if not text:
        return "", ""
    percent = ""
    match = PERCENT_RE.search(text)
    rest = text
    if match:
        percent = f"{match.group(1)}%"
        rest = (text[: match.start()] + text[match.end() :]).strip()
    amount = rest.replace("(", "").replace(")", "").strip()
    return percent, amount


def extract_iid(row: Tag) -> int | None:
    link = row.select_one('td a[href*="getchart.php"]') or row.select_one("td a[data-target]")
    if link is None:
        return None
    href = str(link.get("href") or "")
    match = IID_HREF_RE.search(href)
    if match:
        return int(match.group(1))
    qs_iid = parse_qs(urlparse(href).query).get("iid", [None])[0]
    if qs_iid and str(qs_iid).isdigit():
        return int(qs_iid)
    target = clean_text(str(link.get("data-target") or ""))
    digits = re.sub(r"\D", "", target)
    return int(digits) if digits else None


def _row_classes(row: Tag) -> str:
    return " ".join(row.get("class") or [])


def is_expand_row(row: Tag) -> bool:
    classes = _row_classes(row)
    return "expand" in classes.split() or bool(EXPAND_CLASS_RE.search(classes))


def is_section_row(row: Tag) -> bool:
    return "catsection" in _row_classes(row).split()


def is_header_row(row: Tag, cell_texts: list[str]) -> bool:
    if "header" in _row_classes(row).split():
        return True
    joined = " ".join(cell_texts)
    return "نام خودرو" in joined


def _cell_text(td: Tag) -> str:
    lastprice = td.select_one("span.lastprice")
    if lastprice is not None:
        text = clean_text(lastprice.get_text(" ", strip=True))
        if text:
            return text
    return clean_text(td.get_text(" ", strip=True))


def _section_from_row(row: Tag) -> str:
    heading = row.find(["h2", "h3"])
    if isinstance(heading, Tag):
        return clean_text(heading.get_text(" ", strip=True))
    tds = row.find_all("td", recursive=False)
    if tds:
        return clean_text(tds[0].get_text(" ", strip=True))
    return ""


def _should_skip_section(section: str) -> bool:
    text = clean_text(section)
    return any(hint in text for hint in SKIP_SECTION_HINTS)


def parse_price_html(
    html: str,
    *,
    page_kind: str,
    page_url: str,
    limit: int | None = None,
) -> list[IranJibPriceRow]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.items_table")
    if not tables:
        raise IranJibParseError(f"no table.items_table in {page_kind} html")

    rows: list[IranJibPriceRow] = []
    section = ""
    for table in tables:
        if not isinstance(table, Tag):
            continue
        for tr in table.find_all("tr"):
            if not isinstance(tr, Tag):
                continue
            if is_expand_row(tr):
                continue
            if is_section_row(tr):
                section = _section_from_row(tr)
                continue
            tds = tr.find_all("td", recursive=False)
            texts = [_cell_text(td) for td in tds]
            if is_header_row(tr, texts):
                continue
            if len(tds) < 4:
                continue
            title = texts[0] if texts else ""
            if not title or len(title) > 200:
                continue
            if _should_skip_section(section):
                continue

            market_raw = texts[1] if len(texts) > 1 else ""
            factory_raw = texts[2] if len(texts) > 2 else ""
            change_raw = clean_text(tds[3].get_text(" ", strip=True)) if len(tds) > 3 else ""
            percent_raw, amount_raw = parse_change_fields(change_raw)
            iid = extract_iid(tr)
            status = classify_market_status(market_raw)
            market_toman = parse_toman_amount(market_raw) if status == "AVAILABLE" else None
            factory_toman = parse_toman_amount(factory_raw)

            rows.append(
                IranJibPriceRow(
                    page_kind=page_kind,
                    page_url=page_url,
                    section_raw=section,
                    title_raw=title,
                    iranjib_iid=iid,
                    market_price_raw=market_raw,
                    factory_price_raw=factory_raw,
                    change_amount_raw=amount_raw,
                    change_percent_raw=percent_raw,
                    status_raw=market_raw,
                    status=status,
                    market_price_toman=market_toman,
                    factory_price_toman=factory_toman,
                    raw={
                        "cells": texts,
                        "tr_class": _row_classes(tr),
                        "section": section,
                        "page_kind": page_kind,
                        "iranjib_iid": iid,
                    },
                )
            )
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


def dump_failed_html(
    html: str,
    *,
    page_kind: str,
    url: str,
    reason: str,
) -> Path | None:
    """Persist catalogue HTML when parse yields no rows. Best-effort; never raises."""

    root = Path(os.getenv("IRANJIB_FAILED_HTML_DIR", DEFAULT_FAILED_HTML_DIR) or DEFAULT_FAILED_HTML_DIR)
    safe_kind = re.sub(r"[^a-z0-9_-]+", "_", page_kind.strip().lower()) or "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    header = (
        f"<!-- seenad iranjib dump reason={reason} page_kind={page_kind} "
        f"url={url} dumped_at={stamp} bytes={len(html.encode('utf-8'))} -->\n"
    )
    body = html if len(html.encode("utf-8")) <= FAILED_HTML_MAX_BYTES else html[:FAILED_HTML_MAX_BYTES]
    payload = header + body
    try:
        root.mkdir(parents=True, exist_ok=True)
        latest = root / f"iranjib_fail_{safe_kind}_latest.html"
        stamped = root / f"iranjib_fail_{safe_kind}_{stamp}.html"
        latest.write_text(payload, encoding="utf-8")
        stamped.write_text(payload, encoding="utf-8")
    except OSError:
        LOGGER.exception(
            "iranjib_price failed-html dump error reason=%s page_kind=%s url=%s",
            reason,
            page_kind,
            url,
        )
        return None
    LOGGER.warning(
        "iranjib_price dumped failed html reason=%s page_kind=%s bytes=%s latest=%s stamped=%s",
        reason,
        page_kind,
        len(payload.encode("utf-8")),
        latest,
        stamped,
    )
    return latest


def detect_anti_bot(html: str, status_code: int) -> str | None:
    if status_code in {403, 429, 503}:
        return f"http_{status_code}"
    snippet = html[:8000].lower()
    for marker in ANTI_BOT_MARKERS:
        if marker in snippet:
            return marker
    return None


def fetch_price_page(
    session: requests.Session,
    *,
    url: str,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_attempts: int | None = None,
    backoff_base: float | None = None,
    backoff_cap: float | None = None,
) -> str:
    """GET one catalogue HTML page. Retry timeouts and gateway/CDN errors; fail fast on 4xx like 403."""

    attempts = max_attempts if max_attempts is not None else _env_int(
        "IRANJIB_ZERO_PRICE_HTTP_MAX_ATTEMPTS", DEFAULT_HTTP_MAX_ATTEMPTS, minimum=1
    )
    base = backoff_base if backoff_base is not None else _env_float(
        "IRANJIB_ZERO_PRICE_HTTP_BACKOFF_BASE_S", DEFAULT_HTTP_BACKOFF_BASE_S, minimum=0.1
    )
    cap = backoff_cap if backoff_cap is not None else _env_float(
        "IRANJIB_ZERO_PRICE_HTTP_BACKOFF_CAP_S", DEFAULT_HTTP_BACKOFF_CAP_S, minimum=1.0
    )
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            wait = _backoff_seconds(attempt, base=base, cap=cap)
            LOGGER.warning(
                "iranjib_price fetch network error attempt=%s/%s kind=%s detail=%s — sleep %.1fs url=%s",
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                wait,
                url,
            )
            time.sleep(wait)
            continue

        if response.status_code in RETRYABLE_HTTP_STATUS:
            last_exc = requests.HTTPError(
                f"{response.status_code} Server Error: {response.reason} for url: {response.url}",
                response=response,
            )
            if attempt >= attempts:
                break
            wait = _retry_after_seconds(response, _backoff_seconds(attempt, base=base, cap=cap))
            LOGGER.warning(
                "iranjib_price fetch HTTP %s attempt=%s/%s — sleep %.1fs url=%s",
                response.status_code,
                attempt,
                attempts,
                wait,
                url,
            )
            time.sleep(wait)
            continue

        # Non-retryable HTTP (e.g. 400/401/403) — fail fast, same as Bama hierarchy fetch.
        response.raise_for_status()
        encoding = response.encoding or "utf-8"
        if encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        anti_bot = detect_anti_bot(html, response.status_code)
        if anti_bot:
            last_exc = IranJibTransientError(
                f"iranjib_price anti-bot marker={anti_bot} url={url}"
            )
            if attempt >= attempts:
                break
            wait = _backoff_seconds(attempt, base=base, cap=cap)
            LOGGER.warning(
                "iranjib_price fetch anti-bot attempt=%s/%s marker=%s — sleep %.1fs url=%s",
                attempt,
                attempts,
                anti_bot,
                wait,
                url,
            )
            time.sleep(wait)
            continue
        if attempt > 1:
            LOGGER.info(
                "iranjib_price fetch recovered after attempt=%s/%s url=%s",
                attempt,
                attempts,
                url,
            )
        return html

    assert last_exc is not None
    LOGGER.error("iranjib_price fetch exhausted attempts=%s last_error=%s url=%s", attempts, last_exc, url)
    raise last_exc


def insert_scrape_snapshot_row(db: Session, *, batch_id: str, row: IranJibPriceRow) -> bool:
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import IranJibZeroPriceScrapeSnapshot

    raw_json: dict[str, Any] | None
    try:
        raw_json = dict(row.raw)
    except (TypeError, ValueError):
        raw_json = None

    stmt = pg_insert(IranJibZeroPriceScrapeSnapshot).values(
        batch_id=batch_id,
        page_kind=row.page_kind,
        page_url=row.page_url,
        section_raw=row.section_raw,
        title_raw=row.title_raw,
        iranjib_iid=row.iranjib_iid,
        market_price_raw=row.market_price_raw,
        factory_price_raw=row.factory_price_raw,
        change_amount_raw=row.change_amount_raw,
        change_percent_raw=row.change_percent_raw,
        status_raw=row.status_raw,
        status=row.status,
        market_price_toman=row.market_price_toman,
        factory_price_toman=row.factory_price_toman,
        raw_json=raw_json,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            IranJibZeroPriceScrapeSnapshot.batch_id,
            IranJibZeroPriceScrapeSnapshot.iranjib_iid,
        ],
        index_where=text("iranjib_iid IS NOT NULL"),
    )
    result = db.execute(stmt)
    return result.rowcount != 0


def run_iranjib_zero_price_pipeline(
    *,
    dry_run: bool = False,
    batch_id: str | None = None,
    page_kinds: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> IranJibZeroPriceRunSummary:
    """
    Download both IranJib catalogue pages and persist every data row to
    ``iranjib_zero_price_scrape_snapshot`` (raw ingestion only).

    Both pages must parse at least one row; otherwise the batch is aborted.
    """

    wanted = set(page_kinds) if page_kinds else {kind for kind, _url in PAGE_SOURCES}
    sources = [(kind, url) for kind, url in PAGE_SOURCES if kind in wanted]
    if not sources:
        raise ValueError(f"no IranJib pages selected from {page_kinds!r}")

    logical_batch: str | None = (batch_id or "").strip() or generate_scrape_batch_id()
    persist_batch: str | None = None if dry_run else logical_batch

    LOGGER.info(
        "iranjib_price pipeline start dry_run=%s batch_id=%s pages=%s limit=%s",
        dry_run,
        logical_batch,
        [kind for kind, _url in sources],
        limit,
    )

    http = _http_session()
    parsed_rows: list[IranJibPriceRow] = []
    pages_fetched = 0
    errors_http = 0

    try:
        for index, (kind, url) in enumerate(sources):
            if index > 0:
                time.sleep(random.uniform(0.4, 1.2))
            html = fetch_price_page(http, url=url)
            pages_fetched += 1
            try:
                page_rows = parse_price_html(html, page_kind=kind, page_url=url, limit=limit)
            except IranJibParseError:
                dump_failed_html(html, page_kind=kind, url=url, reason="no_table")
                raise
            if not page_rows:
                dump_failed_html(html, page_kind=kind, url=url, reason="empty_parse")
                raise IranJibTransientError(
                    f"iranjib_price empty parse page_kind={kind} url={url}"
                )
            LOGGER.info(
                "iranjib_price parsed page_kind=%s rows=%s with_iid=%s with_price=%s",
                kind,
                len(page_rows),
                sum(1 for row in page_rows if row.iranjib_iid is not None),
                sum(1 for row in page_rows if row.market_price_toman is not None),
            )
            parsed_rows.extend(page_rows)
    except (requests.RequestException, IranJibTransientError) as exc:
        errors_http += 1
        LOGGER.exception("iranjib_price HTTP failure: %s", exc)
        raise

    items_seen = len(parsed_rows)
    items_with_price = sum(1 for row in parsed_rows if row.market_price_toman is not None)
    items_with_iid = sum(1 for row in parsed_rows if row.iranjib_iid is not None)
    domestic_rows = sum(1 for row in parsed_rows if row.page_kind == "domestic")
    imported_rows = sum(1 for row in parsed_rows if row.page_kind == "imported")
    raw_rows_persisted = 0

    if persist_batch:
        from app.database import SessionLocal

        with SessionLocal() as db:
            try:
                begin_zero_price_batch(
                    db,
                    source=ZeroPriceBatchSource.IRANJIB,
                    batch_id=persist_batch,
                )
                db.commit()
                for row in parsed_rows:
                    inserted = insert_scrape_snapshot_row(db, batch_id=persist_batch, row=row)
                    if inserted:
                        raw_rows_persisted += 1
                        if raw_rows_persisted % RAW_SNAPSHOT_DB_FLUSH_INTERVAL == 0:
                            db.commit()
                            LOGGER.info(
                                "iranjib_price raw snapshot flushed rows=%s batch_id=%s",
                                raw_rows_persisted,
                                persist_batch,
                            )
                db.commit()
                if raw_rows_persisted > 0:
                    complete_zero_price_batch(
                        db,
                        source=ZeroPriceBatchSource.IRANJIB,
                        batch_id=persist_batch,
                        raw_row_count=raw_rows_persisted,
                    )
                    db.commit()
            except Exception:
                db.rollback()
                LOGGER.exception("iranjib_price DB failure batch_id=%s", persist_batch)
                with SessionLocal() as cleanup_db:
                    abort_zero_price_batch(
                        cleanup_db,
                        source=ZeroPriceBatchSource.IRANJIB,
                        batch_id=persist_batch,
                    )
                    cleanup_db.commit()
                raise
        if raw_rows_persisted <= 0:
            with SessionLocal() as cleanup_db:
                abort_zero_price_batch(
                    cleanup_db,
                    source=ZeroPriceBatchSource.IRANJIB,
                    batch_id=persist_batch,
                )
                cleanup_db.commit()
            raise IranJibTransientError(
                f"iranjib_price empty snapshot batch_id={logical_batch} pages={pages_fetched}"
            )

    summary = IranJibZeroPriceRunSummary(
        pages_fetched=pages_fetched,
        items_seen=items_seen,
        items_with_price=items_with_price,
        items_with_iid=items_with_iid,
        domestic_rows=domestic_rows,
        imported_rows=imported_rows,
        errors_http=errors_http,
        raw_rows_persisted=raw_rows_persisted,
        batch_id=logical_batch,
    )
    LOGGER.info(
        "iranjib_price run summary | rows=%s persisted=%s iid=%s priced=%s "
        "domestic=%s imported=%s pages=%s batch_id=%s",
        summary.items_seen,
        summary.raw_rows_persisted,
        summary.items_with_iid,
        summary.items_with_price,
        summary.domestic_rows,
        summary.imported_rows,
        summary.pages_fetched,
        summary.batch_id,
    )
    return summary
