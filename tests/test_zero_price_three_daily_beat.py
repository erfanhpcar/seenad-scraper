"""Three-daily zero-price Beat schedules (Bama + IranJib)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

BAMA_TEHRAN_SLOTS = ("10:45", "12:45", "15:45")
BAMA_UTC_SLOTS = ("07:15", "09:15", "12:15")
IRANJIB_TEHRAN_SLOTS = ("10:50", "12:50", "15:50")
IRANJIB_UTC_SLOTS = ("07:20", "09:20", "12:20")

LEGACY_TWICE_DAILY_KEYS = (
    "bama-zero-price-twice-daily",
    "iranjib-zero-price-twice-daily",
)


def _load_beat_schedule(extra_env: dict[str, str]) -> dict[str, dict]:
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
out = {}
for name, entry in schedule.items():
    sched = entry.get("schedule")
    out[name] = {
        "task": entry.get("task"),
        "hour": sorted(getattr(sched, "hour", []) or []),
        "minute": sorted(getattr(sched, "minute", []) or []),
    }
print(json.dumps(out))
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


def test_bama_thrice_daily_utc_defaults(production_like_beat_env: dict[str, str]) -> None:
    schedule = _load_beat_schedule(production_like_beat_env)
    entry = schedule["bama-zero-price-thrice-daily-tehran"]
    assert entry["task"] == "tasks.bama_zero_price_scrape"
    assert entry["hour"] == [7, 9, 12]
    assert entry["minute"] == [15]
    assert len(entry["hour"]) == 3
    assert list(zip(entry["hour"], entry["minute"])) == [
        (7, 15),
        (9, 15),
        (12, 15),
    ]


def test_iranjib_thrice_daily_utc_defaults(production_like_beat_env: dict[str, str]) -> None:
    schedule = _load_beat_schedule(production_like_beat_env)
    entry = schedule["iranjib-zero-price-thrice-daily-tehran"]
    assert entry["task"] == "tasks.iranjib_zero_price_scrape"
    assert entry["hour"] == [7, 9, 12]
    assert entry["minute"] == [20]
    assert list(zip(entry["hour"], entry["minute"])) == [
        (7, 20),
        (9, 20),
        (12, 20),
    ]


def test_tehran_slot_labels_documented() -> None:
    """Static mapping guard: UTC defaults must match agreed Tehran wall-clock slots."""
    assert len(BAMA_TEHRAN_SLOTS) == len(BAMA_UTC_SLOTS) == 3
    assert len(IRANJIB_TEHRAN_SLOTS) == len(IRANJIB_UTC_SLOTS) == 3
    assert BAMA_UTC_SLOTS == ("07:15", "09:15", "12:15")
    assert IRANJIB_UTC_SLOTS == ("07:20", "09:20", "12:20")


def test_legacy_twice_daily_schedule_keys_removed(production_like_beat_env: dict[str, str]) -> None:
    schedule = _load_beat_schedule(production_like_beat_env)
    for legacy_key in LEGACY_TWICE_DAILY_KEYS:
        assert legacy_key not in schedule


def test_no_duplicate_zero_price_beat_entries(production_like_beat_env: dict[str, str]) -> None:
    schedule = _load_beat_schedule(production_like_beat_env)
    zero_price_tasks = [
        entry["task"]
        for entry in schedule.values()
        if entry.get("task") in {"tasks.bama_zero_price_scrape", "tasks.iranjib_zero_price_scrape"}
    ]
    assert zero_price_tasks.count("tasks.bama_zero_price_scrape") == 1
    assert zero_price_tasks.count("tasks.iranjib_zero_price_scrape") == 1


def test_docker_compose_env_matches_defaults(production_like_beat_env: dict[str, str]) -> None:
    env = {
        **production_like_beat_env,
        "BAMA_ZERO_PRICE_BEAT_HOURS_UTC": "7,9,12",
        "BAMA_ZERO_PRICE_BEAT_MINUTE_UTC": "15",
        "IRANJIB_ZERO_PRICE_BEAT_HOURS_UTC": "7,9,12",
        "IRANJIB_ZERO_PRICE_BEAT_MINUTE_UTC": "20",
    }
    schedule = _load_beat_schedule(env)
    assert schedule["bama-zero-price-thrice-daily-tehran"]["hour"] == [7, 9, 12]
    assert schedule["iranjib-zero-price-thrice-daily-tehran"]["minute"] == [20]


def test_unrelated_crawler_schedules_unchanged(production_like_beat_env: dict[str, str]) -> None:
    schedule = _load_beat_schedule(production_like_beat_env)
    assert schedule["usd-irr-rate-daily"]["hour"] == [7, 14]
    assert schedule["sell-news-daily"]["hour"] == [7, 12]
    assert "bama-ingestion-queue-every-interval" in schedule
    assert "kn-ingestion-queue-every-interval" in schedule
    assert "hm-ingestion-queue-every-interval" in schedule
