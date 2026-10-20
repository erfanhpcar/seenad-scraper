"""Environment flags for zero-price benchmark (scraper-side collector)."""

from __future__ import annotations

import os

# SB-01: scraper-side benchmark DB writes are deprecated; seenad-main owns remediation.
LEGACY_WRITER_COMPAT_ENV: str = "BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED"


class LegacyBenchmarkWriterDisabled(RuntimeError):
    """Scraper benchmark writer blocked until schema compatibility is explicitly verified."""


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", ""}


def benchmark_enabled() -> bool:
    return _flag("BENCHMARK_ENABLED", "0")


def legacy_benchmark_writer_compat_verified() -> bool:
    return _flag(LEGACY_WRITER_COMPAT_ENV, "0")


def legacy_benchmark_writer_allowed() -> bool:
    """True only when both runtime enable and post-migration compatibility opt-in are set."""
    return benchmark_enabled() and legacy_benchmark_writer_compat_verified()


def legacy_benchmark_writer_block_reason() -> str | None:
    if not benchmark_enabled():
        return "BENCHMARK_ENABLED=0"
    if not legacy_benchmark_writer_compat_verified():
        return (
            f"{LEGACY_WRITER_COMPAT_ENV}=0 (SB-01: scraper legacy benchmark writer isolated; "
            "seenad-main is the sole writer until schema compatibility is verified)"
        )
    return None


def assert_legacy_benchmark_writer_may_run() -> None:
    reason = legacy_benchmark_writer_block_reason()
    if reason:
        raise LegacyBenchmarkWriterDisabled(reason)


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
