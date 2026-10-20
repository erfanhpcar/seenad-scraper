"""Text, URL, Jalali date, price, sale-type, and brand/model helpers."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import jdatetime
from bs4 import BeautifulSoup

from src.scraper.sell_news.constants import (
    SALE_TYPE_FOGH,
    SALE_TYPE_FOURI,
    SALE_TYPE_GHORA,
    SALE_TYPE_OTHER,
    SALE_TYPE_PISH,
    SALE_TYPE_SAZMANI,
    SALE_TYPE_VAREDATI,
    SALE_TYPE_VIZHE,
    TEHRAN_TZ,
)

ARABIC_VARIANTS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
WS_RE = re.compile(r"\s+")
ZWNJ = "\u200c"
TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "yclid", "mc_", "_hs")

JALALI_MONTHS: dict[str, int] = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "ابان": 8,
    "آذر": 9,
    "اذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}

COMPANY_NAMES = (
    "ایران خودرو دیزل",
    "ایران خودرو",
    "سایپا",
    "پارس خودرو",
    "مدیران خودرو",
    "کرمان موتور",
    "بهمن موتور",
    "بهمن دیزل",
    "دینا موتور",
    "کوشا خودرو",
    "شتابران خودرو",
    "نامی خودرو",
    "الماس خودرو نامی",
    "آفتاب خودرو",
    "ساتیار خودرو آسیا",
    "انرژی موتور آسیا",
    "انرژی موتور",
    "پیشتاز خودرو جنوب",
    "پیشتاز خودرو",
    "هویار موتور",
    "برساوش",
    "زاوینا موتور",
    "سیبا موتور",
    "فردا موتور",
    "آپام خودرو اروند",
    "پارس ساتر هوشمند",
    "آمیتیس",
    "صنایع خودروسازی پادرا",
    "مهام خودرو پارس",
    "ایستا نگین بینالود",
    "پارسا موتور سامان",
    "هرمس خودرو",
    "سروش موتور",
    "واگن هود",
    "زامیاد",
    "اتونوین",
)

# Longest-first brand prefixes used only to split a raw vehicle phrase.
BRAND_PREFIXES = (
    "ایران خودرو دیزل",
    "ایران خودرو",
    "مدیران خودرو",
    "کرمان موتور",
    "بهمن موتور",
    "بهمن دیزل",
    "فولکس واگن",
    "فولکس‌واگن",
    "بی وای دی",
    "بی‌وای‌دی",
    "ام وی ام",
    "ام‌وی‌ام",
    "فونیکس NEV",
    "اکستریم",
    "فونیکس",
    "تویوتا",
    "هیوندای",
    "هیوندا",
    "نیسان",
    "مزدا",
    "ولوو",
    "اشکودا",
    "اسکودا",
    "چانگان",
    "هاوال",
    "مکسوس",
    "تانک",
    "کیا",
    "گک",
    "نامی",
    "داچیا",
    "آئودی",
    "ائودی",
    "بی ام و",
    "بی‌ام‌و",
    "مرسدس",
    "بنز",
    "جک",
    "کی ام سی",
    "کی‌ام‌سی",
    "لاماری",
    "فیدلیتی",
    "ریرا",
    "دنا",
    "تارا",
    "سورن",
    "رانا",
    "پژو",
    "سمند",
    "کوییک",
    "ساینا",
    "شاهین",
    "اطلس",
    "تیسان",
    "آریسان",
    "اریسان",
    "سایپا",
    "زامیاد",
    "امکو",
    "امپو",
    "امزوم",
    "BYD",
    "MVM",
    "KMC",
    "GAC",
    "VW",
    "IM",
    "Toyota",
    "Nissan",
    "Hyundai",
    "Kia",
    "Honda",
    "Mazda",
    "Volvo",
    "BMW",
    "Audi",
    "Lexus",
    "Mitsubishi",
    "Suzuki",
    "Renault",
    "Peugeot",
    "Volkswagen",
    "Chevrolet",
    "Ford",
    "Skoda",
    "Haval",
    "Changan",
    "JAC",
    "MG",
)

SALE_TYPE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (SALE_TYPE_FOURI, "فروش فوری", re.compile(r"فروش\s*فوری")),
    (SALE_TYPE_PISH, "پیش‌فروش", re.compile(r"پیش[-\u200c\s]*فروش")),
    (SALE_TYPE_FOGH, "فروش فوق‌العاده", re.compile(r"فوق[-\u200c\s]*العاده")),
    (SALE_TYPE_VIZHE, "فروش ویژه", re.compile(r"فروش\s*ویژه|طرح\s*ویژه")),
    (SALE_TYPE_SAZMANI, "فروش سازمانی", re.compile(r"سازمانی")),
    (SALE_TYPE_VAREDATI, "وارداتی", re.compile(r"واردات(?:ی)?")),
    (SALE_TYPE_GHORA, "قرعه‌کشی", re.compile(r"قرعه[-\u200c\s]*کشی")),
)

PLACEHOLDER_PRICE_RE = re.compile(r"^[\s\-–—ـ*]+$")
NUMBER_WORD_RE = re.compile(
    r"(?P<num>[\d۰-۹0-9,\.،/]+)\s*(?P<unit>میلیارد|میلیون|هزار)?"
)


def clean_text(value: str | None, *, keep_zwnj: bool = False) -> str | None:
    if value is None:
        return None
    text = html_lib.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200f", "").replace("\u200e", "")
    if keep_zwnj:
        text = text.replace(ZWNJ, "\u200c")
    else:
        text = text.replace(ZWNJ, " ")
    text = text.translate(PERSIAN_DIGITS).translate(ARABIC_VARIANTS)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_text(soup.get_text(" ", strip=True))


def article_text(value: str | None) -> str | None:
    """Collapse whitespace inside one block. Keep Persian digits and source letters."""

    if value is None:
        return None
    text = html_lib.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200f", "").replace("\u200e", "")
    text = WS_RE.sub(" ", text).strip()
    return text or None


def html_fragment_text(html: str | None) -> str | None:
    """Plain text of a small HTML fragment (dek/excerpt), digits preserved."""

    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return article_text(soup.get_text(" ", strip=True))


def decode_title(value: str | None) -> str:
    return clean_text(value) or ""


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    raw = html_lib.unescape(url.strip())
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = unquote(parts.netloc).lower()
    if host.startswith("www."):
        host_norm = host
    else:
        host_norm = host
    path = unquote(parts.path or "/")
    path = path.replace("//", "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(k.lower().startswith(p) for p in TRACKING_QUERY_PREFIXES)
    ]
    # Article pages do not need remaining query; list pagination is not stored as canonical.
    query = "" if not query_pairs else urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, host_norm, path, query, ""))


def first_sentences(text: str | None, max_sentences: int = 2) -> str | None:
    if not text:
        return None
    cleaned = clean_text(text)
    if not cleaned:
        return None
    parts = re.split(r"(?<=[.!?؟])\s+", cleaned)
    chosen = [p for p in parts if p][:max_sentences]
    out = " ".join(chosen).strip()
    return out or None


def first_article_sentences(text: str | None, max_sentences: int = 2) -> str | None:
    """Lead sentences from structured full_text; Persian digits kept."""

    if not text:
        return None
    cleaned = article_text(text.replace("\n", " "))
    if not cleaned:
        return None
    parts = re.split(r"(?<=[.!?؟])\s+", cleaned)
    chosen = [p for p in parts if p][:max_sentences]
    out = " ".join(chosen).strip()
    return out or None


def parse_jalali_datetime(value: str | None) -> datetime | None:
    """Parse Jalali or ISO timestamps into aware Asia/Tehran datetimes."""

    if not value:
        return None
    text = clean_text(value) or ""
    if not text:
        return None

    iso = _parse_iso_datetime(text)
    if iso is not None:
        return iso

    # 1405/5/24 - 08:15  or 1405/05/24 08:15
    m = re.search(
        r"(?P<y>13\d{2}|14\d{2})[/-](?P<mo>\d{1,2})[/-](?P<d>\d{1,2})"
        r"(?:\s*[-–]?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?)?",
        text,
    )
    if m:
        return _jalali_to_tehran(
            int(m.group("y")),
            int(m.group("mo")),
            int(m.group("d")),
            int(m.group("h") or 0),
            int(m.group("mi") or 0),
            int(m.group("s") or 0),
        )

    # ۲۴ مرداد ۱۴۰۵  or 24 مرداد 1405 - 21:45
    months = "|".join(re.escape(n) for n in JALALI_MONTHS)
    m = re.search(
        rf"(?P<d>\d{{1,2}})\s+(?P<mon>{months})\s+(?P<y>13\d{{2}}|14\d{{2}})"
        rf"(?:\s*[-–]?\s*(?P<h>\d{{1,2}}):(?P<mi>\d{{2}})(?::(?P<s>\d{{2}}))?)?",
        text,
    )
    if m:
        return _jalali_to_tehran(
            int(m.group("y")),
            JALALI_MONTHS[m.group("mon")],
            int(m.group("d")),
            int(m.group("h") or 0),
            int(m.group("mi") or 0),
            int(m.group("s") or 0),
        )
    return None


def _parse_iso_datetime(text: str) -> datetime | None:
    raw = text.replace("Z", "+00:00")
    raw = re.sub(r"\s+", "T", raw, count=1) if re.match(r"\d{4}-\d{2}-\d{2} ", raw) else raw
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", raw)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1).replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TEHRAN_TZ)
    return dt.astimezone(TEHRAN_TZ)


def _jalali_to_tehran(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime | None:
    try:
        gd = jdatetime.datetime(year, month, day, hour, minute, second).togregorian()
    except (ValueError, OverflowError):
        return None
    return gd.replace(tzinfo=TEHRAN_TZ)


def tehran_now() -> datetime:
    return datetime.now(TEHRAN_TZ)


def jalali_batch_id(when: datetime | None = None) -> str:
    when = when or tehran_now()
    jd = jdatetime.datetime.fromgregorian(datetime=when)
    return f"sell-news-{jd.year:04d}-{jd.month:02d}-{jd.day:02d}"


def lookback_cutoff(days: int = 7, when: datetime | None = None) -> datetime:
    return (when or tehran_now()) - timedelta(days=days)


def parse_price(
    raw: str | None,
    *,
    header_hint: str | None = None,
) -> tuple[int | None, str | None]:
    """Return (price_toman, unit_guess). unit_guess is toman|rial|unknown."""

    text = clean_text(raw)
    if not text or PLACEHOLDER_PRICE_RE.match(text):
        return None, None
    header = clean_text(header_hint) or ""
    unit = _guess_unit(text, header)
    amount = _parse_amount_number(text)
    if amount is None:
        return None, unit or "unknown"

    if unit == "rial":
        if amount >= 10:
            toman = int(amount // 10)
            if toman <= 0:
                return None, "rial"
            return toman, "rial"
        return None, "rial"
    if unit == "toman":
        return int(amount), "toman"
    return None, "unknown"


def _guess_unit(text: str, header: str) -> str:
    blob = f"{header} {text}"
    if re.search(r"ریال|ريال", blob):
        return "rial"
    if "تومان" in blob:
        return "toman"
    return "unknown"


def _parse_amount_number(text: str) -> int | None:
    """Parse Persian/English grouped digits, optional میلیارد/میلیون words."""

    lowered = text
    if re.search(r"میلیارد|میلیون|هزار", lowered):
        total = Decimal(0)
        found = False
        for m in NUMBER_WORD_RE.finditer(lowered):
            num = _digits_to_int(m.group("num"))
            if num is None:
                continue
            found = True
            unit = m.group("unit") or ""
            if unit == "میلیارد":
                total += Decimal(num) * Decimal(1_000_000_000)
            elif unit == "میلیون":
                total += Decimal(num) * Decimal(1_000_000)
            elif unit == "هزار":
                total += Decimal(num) * Decimal(1_000)
            else:
                total += Decimal(num)
        if found and total > 0:
            return int(total)

    m = re.search(r"[\d,\.،/]+", lowered)
    if not m:
        return None
    return _digits_to_int(m.group(0))


def _digits_to_int(token: str) -> int | None:
    s = token.translate(PERSIAN_DIGITS).replace("،", ",").replace("/", ",").strip()
    if not s:
        return None
    # 12.400.000.000 (dot thousands) vs 12.4 (decimal — not used for car prices)
    if s.count(".") >= 2 and "," not in s:
        s = s.replace(".", "")
    elif s.count(",") >= 1:
        s = s.replace(",", "")
        if s.count(".") == 1 and len(s.split(".")[-1]) == 3:
            s = s.replace(".", "")
        else:
            s = s.split(".")[0]
    else:
        s = s.replace(",", "")
    s = re.sub(r"[^\d]", "", s)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def classify_sale_type(*blobs: str | None) -> tuple[str, str | None]:
    title = blobs[0] if blobs else ""
    ordered = list(blobs)
    # Title has priority: scan title first, then the rest.
    for blob in ordered:
        text = clean_text(blob) or ""
        if not text:
            continue
        for code, raw, pat in SALE_TYPE_PATTERNS:
            if pat.search(text):
                return code, raw
    return SALE_TYPE_OTHER, None


def extract_company_name(*blobs: str | None) -> str | None:
    hay = " ".join(clean_text(b) or "" for b in blobs)
    if not hay:
        return None
    for name in COMPANY_NAMES:
        if name in hay:
            return name
    return None


def split_brand_model(raw: str | None) -> tuple[str | None, str | None]:
    """Split a vehicle phrase into brand/model only when a known prefix is present."""

    text = clean_text(raw)
    if not text:
        return None, None
    compact = text.replace("‌", " ")
    for brand in BRAND_PREFIXES:
        if compact.lower().startswith(brand.lower() + " ") or compact.lower() == brand.lower():
            rest = compact[len(brand) :].strip(" -–—/")
            return brand, (rest or None)
        # leading English brand
        if compact.upper().startswith(brand.upper() + " "):
            rest = compact[len(brand) :].strip(" -–—/")
            return brand, (rest or None)
    return None, text


def is_four_digit_year(value: str | None) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    m = re.fullmatch(r"(13\d{2}|14\d{2}|19\d{2}|20\d{2})", text)
    if not m:
        return None
    return int(m.group(1))


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
