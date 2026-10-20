"""
Fetch Iran free-market USD/IRR from TGJU and persist one daily snapshot.

Uses ``price_dollar_rl`` (دلار آزاد), not official/CBI/ICE rates.
Seenad reads ``usd_irr_rate`` at 11:15 Asia/Tehran (morning snapshot) and
the 18:00 Tehran Beat still refreshes the same daily row.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Mapping
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import UsdIrrRate

LOGGER: Final = logging.getLogger(__name__)

TEHRAN_TZ: Final = ZoneInfo("Asia/Tehran")
DEFAULT_TGJU_URL: Final[str] = "https://call5.tgju.org/ajax.json"
DEFAULT_TGJU_HISTORY_URL: Final[str] = (
    "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl"
)
FREE_MARKET_INSTRUMENT_KEY: Final[str] = "price_dollar_rl"
SOURCE_NAME: Final[str] = "tgju"
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 30.0
DEFAULT_HISTORY_TIMEOUT_S: Final[float] = 60.0
_HTML_TAG_RE: Final = re.compile(r"<[^>]+>")
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class UsdIrrRateFetchError(Exception):
    """Raised when TGJU free-market USD cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class TgjuFreeMarketUsdQuote:
    rate_irr: int
    rate_toman: int
    high_irr: int | None
    low_irr: int | None
    change_irr: int | None
    change_percent: float | None
    quoted_at: datetime | None
    instrument_key: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class UsdIrrRateRunSummary:
    success: bool
    as_of_date: str
    rate_irr: int
    rate_toman: int
    source: str
    instrument_key: str
    quoted_at: str | None
    row_id: int | None
    action: str = "none"  # insert | update | skip | none
    carried_forward: bool = False
    quote_date: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_s: float | None = None
    error: str | None = None

    def as_celery_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "as_of_date": self.as_of_date,
            "rate_irr": self.rate_irr,
            "rate_toman": self.rate_toman,
            "source": self.source,
            "instrument_key": self.instrument_key,
            "quoted_at": self.quoted_at,
            "row_id": self.row_id,
            "action": self.action,
            "carried_forward": self.carried_forward,
            "quote_date": self.quote_date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class UsdIrrRateBackfillSummary:
    success: bool
    days_requested: int
    rows_from_source: int
    rows_upserted: int
    first_as_of_date: str | None
    last_as_of_date: str | None
    source: str
    instrument_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "days_requested": self.days_requested,
            "rows_from_source": self.rows_from_source,
            "rows_upserted": self.rows_upserted,
            "first_as_of_date": self.first_as_of_date,
            "last_as_of_date": self.last_as_of_date,
            "source": self.source,
            "instrument_key": self.instrument_key,
        }


def tehran_today() -> date:
    return datetime.now(TEHRAN_TZ).date()


def parse_tgju_price_to_irr(value: Any) -> int:
    """Parse TGJU price strings like ``1,892,000`` into a positive IRR integer."""

    if value is None:
        raise UsdIrrRateFetchError("TGJU price value is missing")
    if isinstance(value, bool):
        raise UsdIrrRateFetchError(f"TGJU price value has invalid type: {type(value)!r}")
    if isinstance(value, int):
        if value <= 0:
            raise UsdIrrRateFetchError(f"TGJU price must be positive, got {value}")
        return value
    if isinstance(value, float):
        if value <= 0:
            raise UsdIrrRateFetchError(f"TGJU price must be positive, got {value}")
        return int(value)

    text = str(value).strip()
    for ch in ("\u066c", ",", "٬", " ", "\u200f", "\u200e"):
        text = text.replace(ch, "")
    if not text:
        raise UsdIrrRateFetchError("TGJU price string is empty")
    try:
        rate = int(text)
    except ValueError as exc:
        raise UsdIrrRateFetchError(f"Cannot parse TGJU price: {value!r}") from exc
    if rate <= 0:
        raise UsdIrrRateFetchError(f"TGJU price must be positive, got {rate}")
    return rate


def _optional_irr(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return parse_tgju_price_to_irr(value)
    except UsdIrrRateFetchError:
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_quoted_at(raw: Mapping[str, Any]) -> datetime | None:
    ts = raw.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        # TGJU timestamps are wall-clock Tehran without offset, e.g. "2026-08-05 13:22:07"
        naive = datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=TEHRAN_TZ)
    except ValueError:
        return None


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
        }
    )
    return session


def fetch_tgju_free_market_usd(*, timeout_s: float | None = None) -> TgjuFreeMarketUsdQuote:
    """
    GET TGJU ajax feed and extract free-market USD (``price_dollar_rl``).

    Raises ``UsdIrrRateFetchError`` on HTTP/parse failure.
    """

    url = os.getenv("USD_IRR_RATE_TGJU_URL", DEFAULT_TGJU_URL).strip() or DEFAULT_TGJU_URL
    instrument_key = (
        os.getenv("USD_IRR_RATE_INSTRUMENT_KEY", FREE_MARKET_INSTRUMENT_KEY).strip()
        or FREE_MARKET_INSTRUMENT_KEY
    )
    if timeout_s is None:
        raw_timeout = os.getenv("USD_IRR_RATE_TIMEOUT_SECONDS", "").strip()
        timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_REQUEST_TIMEOUT_S

    try:
        response = _http_session().get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise UsdIrrRateFetchError(f"TGJU request failed: {exc}") from exc
    except ValueError as exc:
        raise UsdIrrRateFetchError(f"TGJU response is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise UsdIrrRateFetchError("TGJU JSON root must be an object")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise UsdIrrRateFetchError("TGJU JSON missing 'current' object")
    instrument = current.get(instrument_key)
    if not isinstance(instrument, dict):
        raise UsdIrrRateFetchError(
            f"TGJU free-market instrument missing: {instrument_key!r} "
            "(refusing official/ICE keys)"
        )

    rate_irr = parse_tgju_price_to_irr(instrument.get("p"))
    rate_toman = rate_irr // 10

    return TgjuFreeMarketUsdQuote(
        rate_irr=rate_irr,
        rate_toman=rate_toman,
        high_irr=_optional_irr(instrument.get("h")),
        low_irr=_optional_irr(instrument.get("l")),
        change_irr=_optional_irr(instrument.get("d")),
        change_percent=_optional_float(instrument.get("dp")),
        quoted_at=_parse_quoted_at(instrument),
        instrument_key=instrument_key,
        raw=dict(instrument),
    )


def upsert_usd_irr_rate(
    db: Session,
    *,
    quote: TgjuFreeMarketUsdQuote,
    as_of_date: date,
    source: str = SOURCE_NAME,
) -> UsdIrrRate:
    """Insert or update the daily row for ``(as_of_date, source)``."""

    now_utc = datetime.now(timezone.utc)
    values = {
        "as_of_date": as_of_date,
        "rate_irr": quote.rate_irr,
        "rate_toman": quote.rate_toman,
        "source": source,
        "instrument_key": quote.instrument_key,
        "quoted_at": quote.quoted_at,
        "high_irr": quote.high_irr,
        "low_irr": quote.low_irr,
        "change_irr": quote.change_irr,
        "change_percent": quote.change_percent,
        "raw_json": dict(quote.raw),
        "created_at": now_utc,
        "updated_at": now_utc,
    }
    stmt = pg_insert(UsdIrrRate).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_usd_irr_rate_as_of_date_source",
        set_={
            "rate_irr": stmt.excluded.rate_irr,
            "rate_toman": stmt.excluded.rate_toman,
            "instrument_key": stmt.excluded.instrument_key,
            "quoted_at": stmt.excluded.quoted_at,
            "high_irr": stmt.excluded.high_irr,
            "low_irr": stmt.excluded.low_irr,
            "change_irr": stmt.excluded.change_irr,
            "change_percent": stmt.excluded.change_percent,
            "raw_json": stmt.excluded.raw_json,
            "updated_at": now_utc,
        },
    ).returning(UsdIrrRate.id)

    row_id = db.execute(stmt).scalar_one()
    db.commit()
    row = db.get(UsdIrrRate, row_id)
    if row is None:
        raise UsdIrrRateFetchError(f"Upsert succeeded but row id={row_id} not found")
    return row


def _quote_calendar_date(quote: TgjuFreeMarketUsdQuote) -> date | None:
    if quote.quoted_at is None:
        return None
    return quote.quoted_at.astimezone(TEHRAN_TZ).date()


def load_last_stored_quote(*, source: str = SOURCE_NAME) -> tuple[date, TgjuFreeMarketUsdQuote] | None:
    """Latest persisted free-market row, used when TGJU has no usable live quote."""

    with SessionLocal() as db:
        row = db.scalar(
            select(UsdIrrRate)
            .where(UsdIrrRate.source == source)
            .where(UsdIrrRate.instrument_key == FREE_MARKET_INSTRUMENT_KEY)
            .order_by(UsdIrrRate.as_of_date.desc(), UsdIrrRate.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        quote = TgjuFreeMarketUsdQuote(
            rate_irr=int(row.rate_irr),
            rate_toman=int(row.rate_toman),
            high_irr=row.high_irr,
            low_irr=row.low_irr,
            change_irr=row.change_irr,
            change_percent=float(row.change_percent) if row.change_percent is not None else None,
            quoted_at=row.quoted_at,
            instrument_key=row.instrument_key or FREE_MARKET_INSTRUMENT_KEY,
            raw=dict(row.raw_json or {}) | {
                "carried_forward_from_as_of_date": row.as_of_date.isoformat(),
            },
        )
        return row.as_of_date, quote


def _existing_today_row(as_of: date, *, source: str = SOURCE_NAME) -> UsdIrrRate | None:
    with SessionLocal() as db:
        return db.scalar(
            select(UsdIrrRate).where(
                UsdIrrRate.as_of_date == as_of,
                UsdIrrRate.source == source,
            )
        )


def run_usd_irr_rate_pipeline(*, dry_run: bool = False) -> UsdIrrRateRunSummary:
    """Fetch free-market USD from TGJU and persist today's Tehran-date snapshot.

    Always targets ``as_of_date = today (Asia/Tehran)``. If TGJU has not printed a
    today-dated quote yet, the last valid live/stored rate is written onto today's
    row (``carried_forward=True``). If nothing can be written, logs that today's
    row was not created.
    """

    started = datetime.now(timezone.utc)
    as_of = tehran_today()
    carried_forward = False
    quote: TgjuFreeMarketUsdQuote | None = None
    quote_date: date | None = None
    fetch_error: str | None = None

    try:
        quote = fetch_tgju_free_market_usd()
        quote_date = _quote_calendar_date(quote)
        if quote_date is not None and quote_date < as_of:
            carried_forward = True
            LOGGER.warning(
                "TGJU live quote is dated %s; carrying last valid rate onto as_of_date=%s",
                quote_date.isoformat(),
                as_of.isoformat(),
            )
        elif quote_date is None:
            LOGGER.info(
                "TGJU live quote has no timestamp; treating rate_irr=%s as today's snapshot as_of_date=%s",
                quote.rate_irr,
                as_of.isoformat(),
            )
    except UsdIrrRateFetchError as exc:
        fetch_error = str(exc)
        LOGGER.warning("TGJU live fetch failed: %s", fetch_error)
        stored = load_last_stored_quote()
        if stored is None:
            finished = datetime.now(timezone.utc)
            LOGGER.error(
                "usd_irr_rate today's row NOT created as_of_date=%s (no live quote, no prior row)",
                as_of.isoformat(),
            )
            return UsdIrrRateRunSummary(
                success=False,
                as_of_date=as_of.isoformat(),
                rate_irr=0,
                rate_toman=0,
                source=SOURCE_NAME,
                instrument_key=FREE_MARKET_INSTRUMENT_KEY,
                quoted_at=None,
                row_id=None,
                action="none",
                carried_forward=False,
                quote_date=None,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                elapsed_s=round((finished - started).total_seconds(), 3),
                error=fetch_error,
            )
        prior_date, quote = stored
        quote_date = prior_date
        if prior_date == as_of:
            existing = _existing_today_row(as_of)
            finished = datetime.now(timezone.utc)
            LOGGER.warning(
                "TGJU live fetch failed; keeping existing today row as_of_date=%s rate_irr=%s row_id=%s",
                as_of.isoformat(),
                quote.rate_irr,
                existing.id if existing else None,
            )
            return UsdIrrRateRunSummary(
                success=True,
                as_of_date=as_of.isoformat(),
                rate_irr=quote.rate_irr,
                rate_toman=quote.rate_toman,
                source=SOURCE_NAME,
                instrument_key=quote.instrument_key,
                quoted_at=quote.quoted_at.isoformat() if quote.quoted_at else None,
                row_id=existing.id if existing else None,
                action="skip",
                carried_forward=False,
                quote_date=quote_date.isoformat(),
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                elapsed_s=round((finished - started).total_seconds(), 3),
                error=fetch_error,
            )
        carried_forward = True
        LOGGER.warning(
            "TGJU live fetch failed; carrying last stored rate from %s onto as_of_date=%s rate_irr=%s",
            prior_date.isoformat(),
            as_of.isoformat(),
            quote.rate_irr,
        )

    assert quote is not None
    existed = _existing_today_row(as_of) is not None
    action = "update" if existed else "insert"

    LOGGER.info(
        "usd_irr_rate fetched source=%s key=%s rate_irr=%s rate_toman=%s as_of_date=%s "
        "quote_date=%s carried_forward=%s action=%s dry_run=%s",
        SOURCE_NAME,
        quote.instrument_key,
        quote.rate_irr,
        quote.rate_toman,
        as_of.isoformat(),
        quote_date.isoformat() if quote_date else None,
        carried_forward,
        action,
        dry_run,
    )

    row_id: int | None = None
    rate_irr = quote.rate_irr
    rate_toman = quote.rate_toman
    quoted_at = quote.quoted_at
    instrument_key = quote.instrument_key
    source = SOURCE_NAME

    if not dry_run:
        persist_quote = quote
        if carried_forward:
            persist_quote = TgjuFreeMarketUsdQuote(
                rate_irr=quote.rate_irr,
                rate_toman=quote.rate_toman,
                high_irr=quote.high_irr,
                low_irr=quote.low_irr,
                change_irr=quote.change_irr,
                change_percent=quote.change_percent,
                quoted_at=quote.quoted_at,
                instrument_key=quote.instrument_key,
                raw=dict(quote.raw) | {
                    "carried_forward": True,
                    "as_of_date": as_of.isoformat(),
                    "quote_date": quote_date.isoformat() if quote_date else None,
                },
            )
        with SessionLocal() as db:
            row = upsert_usd_irr_rate(db, quote=persist_quote, as_of_date=as_of, source=SOURCE_NAME)
        row_id = row.id
        rate_irr = row.rate_irr
        rate_toman = row.rate_toman
        quoted_at = row.quoted_at
        instrument_key = row.instrument_key
        source = row.source

    finished = datetime.now(timezone.utc)
    LOGGER.info(
        "usd_irr_rate done success=true as_of_date=%s action=%s insert=%s update=%s "
        "carried_forward=%s row_id=%s elapsed_s=%.3f",
        as_of.isoformat(),
        action,
        1 if action == "insert" else 0,
        1 if action == "update" else 0,
        carried_forward,
        row_id,
        (finished - started).total_seconds(),
    )
    return UsdIrrRateRunSummary(
        success=True,
        as_of_date=as_of.isoformat(),
        rate_irr=rate_irr,
        rate_toman=rate_toman,
        source=source,
        instrument_key=instrument_key,
        quoted_at=quoted_at.isoformat() if quoted_at else None,
        row_id=row_id,
        action=action if not dry_run else "dry_run",
        carried_forward=carried_forward,
        quote_date=quote_date.isoformat() if quote_date else None,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        elapsed_s=round((finished - started).total_seconds(), 3),
        error=fetch_error,
    )


def _strip_html(value: Any) -> str:
    return _HTML_TAG_RE.sub("", str(value)).strip()


def _parse_history_change_percent(value: Any) -> float | None:
    text = _strip_html(value).replace("%", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_tgju_free_market_usd_history(
    *,
    days: int = 30,
    timeout_s: float | None = None,
) -> list[tuple[date, TgjuFreeMarketUsdQuote]]:
    """
    Fetch TGJU daily history for free-market USD (``price_dollar_rl``).

    TGJU history row layout:
    ``[open, low, high, close, change, change%, gregorian_date, jalali_date]``

    Uses **close** as ``rate_irr`` (قیمت پایانی). Only calendar days within the
    last ``days`` window are returned; weekends/holidays may be absent.
    """

    if days < 1:
        raise UsdIrrRateFetchError(f"days must be >= 1, got {days}")

    url = (
        os.getenv("USD_IRR_RATE_TGJU_HISTORY_URL", DEFAULT_TGJU_HISTORY_URL).strip()
        or DEFAULT_TGJU_HISTORY_URL
    )
    if timeout_s is None:
        raw_timeout = os.getenv("USD_IRR_RATE_HISTORY_TIMEOUT_SECONDS", "").strip()
        timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_HISTORY_TIMEOUT_S

    try:
        response = _http_session().get(url, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise UsdIrrRateFetchError(f"TGJU history request failed: {exc}") from exc
    except ValueError as exc:
        raise UsdIrrRateFetchError(f"TGJU history response is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise UsdIrrRateFetchError("TGJU history JSON missing 'data' array")

    cutoff = tehran_today() - timedelta(days=days)
    by_date: dict[date, TgjuFreeMarketUsdQuote] = {}

    for row in payload["data"]:
        if not isinstance(row, list) or len(row) < 7:
            continue
        try:
            as_of = datetime.strptime(str(row[6]).replace("/", "-"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if as_of < cutoff:
            continue

        try:
            open_irr = parse_tgju_price_to_irr(row[0])
            low_irr = parse_tgju_price_to_irr(row[1])
            high_irr = parse_tgju_price_to_irr(row[2])
            close_irr = parse_tgju_price_to_irr(row[3])
        except UsdIrrRateFetchError:
            continue

        change_irr = _optional_irr(_strip_html(row[4])) if len(row) > 4 else None
        change_percent = _parse_history_change_percent(row[5]) if len(row) > 5 else None
        jalali = str(row[7]) if len(row) > 7 else None
        quoted_at = datetime(as_of.year, as_of.month, as_of.day, 18, 0, tzinfo=TEHRAN_TZ)

        by_date[as_of] = TgjuFreeMarketUsdQuote(
            rate_irr=close_irr,
            rate_toman=close_irr // 10,
            high_irr=high_irr,
            low_irr=low_irr,
            change_irr=change_irr,
            change_percent=change_percent,
            quoted_at=quoted_at,
            instrument_key=FREE_MARKET_INSTRUMENT_KEY,
            raw={
                "open": open_irr,
                "low": low_irr,
                "high": high_irr,
                "close": close_irr,
                "change": change_irr,
                "change_percent": change_percent,
                "gregorian_date": as_of.isoformat(),
                "jalali_date": jalali,
                "source_row": row,
            },
        )

    return sorted(by_date.items(), key=lambda item: item[0])


def run_usd_irr_rate_history_backfill(
    *,
    days: int = 30,
    dry_run: bool = False,
) -> UsdIrrRateBackfillSummary:
    """Upsert free-market USD daily closes for the last ``days`` calendar days."""

    history = fetch_tgju_free_market_usd_history(days=days)
    LOGGER.info(
        "usd_irr_rate history fetched days=%s rows=%s dry_run=%s",
        days,
        len(history),
        dry_run,
    )

    if dry_run or not history:
        return UsdIrrRateBackfillSummary(
            success=True,
            days_requested=days,
            rows_from_source=len(history),
            rows_upserted=0,
            first_as_of_date=history[0][0].isoformat() if history else None,
            last_as_of_date=history[-1][0].isoformat() if history else None,
            source=SOURCE_NAME,
            instrument_key=FREE_MARKET_INSTRUMENT_KEY,
        )

    upserted = 0
    with SessionLocal() as db:
        for as_of, quote in history:
            upsert_usd_irr_rate(db, quote=quote, as_of_date=as_of, source=SOURCE_NAME)
            upserted += 1

    return UsdIrrRateBackfillSummary(
        success=True,
        days_requested=days,
        rows_from_source=len(history),
        rows_upserted=upserted,
        first_as_of_date=history[0][0].isoformat(),
        last_as_of_date=history[-1][0].isoformat(),
        source=SOURCE_NAME,
        instrument_key=FREE_MARKET_INSTRUMENT_KEY,
    )
