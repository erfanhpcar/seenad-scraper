"""SB-01: legacy scraper benchmark writer isolation (beat schedule + runtime guard)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _beat_schedule_keys(extra_env: dict[str, str]) -> list[str]:
    """Load celery beat schedule in a subprocess with a fresh module import."""
    payload = json.dumps(extra_env)
    script = """
import json
import os
import importlib
import app.celery_app as celery_module

for key, value in json.loads(os.environ["BEAT_TEST_ENV"]).items():
    os.environ[key] = str(value)

importlib.reload(celery_module)
schedule = celery_module.celery_app.conf.beat_schedule or {}
print(json.dumps(sorted(schedule.keys())))
"""
    env = os.environ.copy()
    env["BEAT_TEST_ENV"] = payload
    env.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/0")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip())


@pytest.fixture
def production_like_beat_env() -> dict[str, str]:
    return {
        "BAMA_INGESTION_BEAT_ENABLED": "1",
        "BAMA_DISCOVERY_BEAT_ENABLED": "1",
        "HM_INGESTION_BEAT_ENABLED": "1",
        "KN_INGESTION_BEAT_ENABLED": "1",
        "LISTING_FEEDER_BEAT_ENABLED": "1",
        "BAMA_ZERO_PRICE_BEAT_ENABLED": "1",
        "IRANJIB_ZERO_PRICE_BEAT_ENABLED": "1",
        "USD_IRR_RATE_BEAT_ENABLED": "1",
        "SELL_NEWS_BEAT_ENABLED": "1",
        "DATA_RETENTION_BEAT_ENABLED": "1",
        "MAPPING_PIPELINE_BEAT_ENABLED": "0",
        "ZERO_PRICE_BENCHMARK_BEAT_ENABLED": "0",
    }


def test_zero_price_benchmark_beat_disabled_by_default(production_like_beat_env: dict[str, str]) -> None:
    keys = _beat_schedule_keys(production_like_beat_env)
    assert "run-zero-price-benchmark-0945-1545-tehran" not in keys


def test_zero_price_benchmark_beat_opt_in_only(production_like_beat_env: dict[str, str]) -> None:
    enabled = {**production_like_beat_env, "ZERO_PRICE_BENCHMARK_BEAT_ENABLED": "1"}
    keys = _beat_schedule_keys(enabled)
    assert "run-zero-price-benchmark-0945-1545-tehran" in keys


def test_unrelated_beat_tasks_remain_when_benchmark_disabled(
    production_like_beat_env: dict[str, str],
) -> None:
    keys = _beat_schedule_keys(production_like_beat_env)
    expected = {
        "bama-ingestion-queue-every-interval",
        "bama-discovery-every-interval",
        "hm-ingestion-queue-every-interval",
        "kn-ingestion-queue-every-interval",
        "listing-feeder-every-interval",
        "bama-zero-price-thrice-daily-tehran",
        "iranjib-zero-price-thrice-daily-tehran",
        "sell-news-daily",
    }
    assert expected.issubset(set(keys))


def test_legacy_writer_guard_requires_compat_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.zero_price_benchmark.config import (
        LegacyBenchmarkWriterDisabled,
        assert_legacy_benchmark_writer_may_run,
        legacy_benchmark_writer_allowed,
    )

    monkeypatch.setenv("BENCHMARK_ENABLED", "1")
    monkeypatch.delenv("BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED", raising=False)
    assert not legacy_benchmark_writer_allowed()
    with pytest.raises(LegacyBenchmarkWriterDisabled):
        assert_legacy_benchmark_writer_may_run()

    monkeypatch.setenv("BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED", "1")
    assert legacy_benchmark_writer_allowed()
    assert_legacy_benchmark_writer_may_run()
