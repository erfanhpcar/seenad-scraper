"""Environment flags for zero-price benchmark (scraper-side collector)."""

from __future__ import annotations

import os


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", ""}


def benchmark_enabled() -> bool:
    return _flag("BENCHMARK_ENABLED", "0")


def daily_limit() -> int | None:
    raw = os.getenv("BENCHMARK_DAILY_LIMIT", "0").strip()
    if not raw or raw == "0":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def hm_price_mode() -> str:
    """catalog (default) | http"""
    return os.getenv("HAMRAH_MECHANIC_BENCHMARK_PRICE_MODE", "catalog").strip().lower()


def hm_price_url() -> str | None:
    raw = os.getenv("HAMRAH_MECHANIC_BENCHMARK_PRICE_URL", "").strip()
    return raw or None


def seenad_valuation_url() -> str | None:
    raw = os.getenv("SEENAD_BENCHMARK_VALUATION_URL", "").strip()
    return raw or None
