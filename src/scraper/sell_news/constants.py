"""Shared constants for the sell-news crawler."""

from __future__ import annotations

from zoneinfo import ZoneInfo

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

SOURCE_KHODRO45 = "khodro45"
SOURCE_CAR_IR = "car.ir"
SOURCE_KHODROBANK = "khodrobank"
SOURCE_AUTONOVIN = "autonovin"
SOURCE_MEGFA = "megfa"
SOURCES = (
    SOURCE_KHODRO45,
    SOURCE_KHODROBANK,
    SOURCE_CAR_IR,
    SOURCE_AUTONOVIN,
    SOURCE_MEGFA,
)

CATEGORY_DEFAULT = "بخشنامه"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 45.0
MAX_RETRIES = 3
DELAY_MIN_S = 0.5
DELAY_MAX_S = 1.5

DAILY_LOOKBACK_DAYS = 7
DAILY_LIST_PAGES = 3

SALE_TYPE_FOURI = "fouri"
SALE_TYPE_PISH = "pish_forosh"
SALE_TYPE_FOGH = "fogh_olade"
SALE_TYPE_VIZHE = "vizhe"
SALE_TYPE_SAZMANI = "sazmani"
SALE_TYPE_VAREDATI = "varedati"
SALE_TYPE_GHORA = "ghorakeshi"
SALE_TYPE_OTHER = "other"

EXTRACT_HTML_TABLE = "html_table"
EXTRACT_KEY_VALUE = "key_value_table"
EXTRACT_PROSE = "prose_fallback"

CONTENT_BLOCKS_VERSION = 1

KHODROBANK_KEYWORDS = (
    "فروش",
    "فوق العاده",
    "فوق‌العاده",
    "ویژه",
    "شرایط",
)

# RSS "فروش" alone is noisy (فراجا online trading, etc.).
KHODROBANK_RSS_STRONG = (
    "شرایط فروش",
    "فروش فوری",
    "پیش فروش",
    "پیش‌فروش",
    "فوق العاده",
    "فوق‌العاده",
    "فروش ویژه",
    "بخشنامه",
    "ثبت نام",
    "ثبت‌نام",
    "قیمت قطعی",
    "جدول",
)

KHODROBANK_RSS_NOISE = (
    "خرید و فروش آنلاین",
    "فراجا",
    "سامانه معاملات",
    "نمایش عمومی",
    "بررسی عوامل",
    "مشخصات و جزئیات",
    "در راه ایران",
    "معرفی شد",
)
