"""Hourly isolated charging crawler: publish one complete snapshot then notify SeenAd."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid

from redis import Redis

from app.celery_app import celery_app
from src.scraper.ev_charging_live import (
    extract_snapshot, notify_seenad, publish_snapshot, snapshot_path,
)

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
        try:
            notify_seenad(snapshot, digest)
            return {"status": "notified", "sha256": digest, "counts": snapshot["counts"]}
        except Exception:
            # No re-crawl just to retry transport of an already complete snapshot.
            LOGGER.warning("ev_charging.webhook_delivery_failed; retry queued", exc_info=True)
            ev_charging_notify_latest.apply_async(
                args=[digest], countdown=60, queue="ev_charging_tasks", expires=3000,
            )
            return {
                "status": "published_notification_retry",
                "sha256": digest, "counts": snapshot["counts"],
            }
    except Exception:
        LOGGER.exception("ev_charging.hourly_crawl_failed; latest valid snapshot preserved")
        raise
    finally:
        redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, _LOCK_KEY, token,
        )


@celery_app.task(name="tasks.ev_charging_notify_latest", bind=True, max_retries=5)
def ev_charging_notify_latest(self, expected_sha256: str) -> dict:
    """Retry webhook delivery of ONLY the current complete file, never an old version."""
    if os.getenv("EV_CHARGING_HOURLY_ENABLED", "0").strip().lower() not in ("1", "true"):
        return {"status": "disabled"}
    source = snapshot_path()
    if not source.is_file():
        return {"status": "missing_snapshot"}
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        # Newer crawl replaced the single file. Its own webhook supersedes us.
        return {"status": "superseded"}
    payload = json.loads(raw)
    try:
        notify_seenad(payload, expected_sha256)
    except Exception as exc:
        raise self.retry(
            exc=exc, countdown=min(60 * 2 ** self.request.retries, 600),
        ) from exc
    return {"status": "notified", "sha256": expected_sha256}
