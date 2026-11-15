"""Hourly isolated charging crawler: publish one complete snapshot then notify SeenAd."""
from __future__ import annotations

import logging
import os
import uuid

from redis import Redis

from app.celery_app import celery_app
from src.scraper.ev_charging_live import extract_snapshot, notify_seenad, publish_snapshot

LOGGER = logging.getLogger(__name__)
_LOCK_KEY = "lock:emapna:hourly_complete_snapshot"


@celery_app.task(name="tasks.ev_charging_hourly_crawl", soft_time_limit=3000, time_limit=3150)
def ev_charging_hourly_crawl() -> dict:
    if os.getenv("EV_CHARGING_HOURLY_ENABLED", "0").strip().lower() not in ("1", "true"):
        return {"status": "disabled"}

    redis_url = os.getenv("REDIS_URL") or os.getenv("CELERY_BROKER_URL")
    if not redis_url:
        raise RuntimeError("Redis URL is required for EV charging overlap lock")
    redis = Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
    token = uuid.uuid4().hex
    if not redis.set(_LOCK_KEY, token, nx=True, ex=3300):
        return {"status": "already_running"}
    try:
        snapshot = extract_snapshot()
        digest, _ = publish_snapshot(snapshot)
        # Notify only after all details succeeded and atomic publication ended.
        notify_seenad(snapshot, digest)
        return {"status": "notified", "sha256": digest, "counts": snapshot["counts"]}
    except Exception:
        LOGGER.exception("ev_charging.hourly_crawl_failed; latest valid snapshot preserved")
        raise
    finally:
        redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, _LOCK_KEY, token,
        )
