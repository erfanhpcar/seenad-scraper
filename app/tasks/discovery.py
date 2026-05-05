"""Periodic discovery of Bama listing URLs into Redis for ingestion."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin, urlparse

import redis
import requests
from bs4 import BeautifulSoup

from app.celery_app import celery_app
from app.scrapers.bama_detail_parser import BAMA_DOMAIN, _random_headers

LOGGER = logging.getLogger(__name__)

_DETAIL_PATH_RE = re.compile(r"/car/detail-[^\s\"'<>?#]+", re.IGNORECASE)


def _queue_key() -> str:
    return os.getenv("SEENAD_BAMA_URL_QUEUE_KEY", "seenad:bama:url_queue").strip() or "seenad:bama:url_queue"


def _seen_key() -> str:
    return os.getenv("SEENAD_BAMA_URL_SEEN_SET_KEY", "seenad:bama:url_seen").strip() or "seenad:bama:url_seen"


def _listing_urls_to_fetch() -> list[str]:
    raw = os.getenv("BAMA_DISCOVERY_LIST_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return ["https://bama.ir/car/all"]


def _normalize_candidate_url(raw_href: str) -> str | None:
    href = raw_href.strip()
    if not href or "/car/detail-" not in href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    full = urljoin(f"{BAMA_DOMAIN}/", href)
    parsed = urlparse(full)
    if not parsed.path.startswith("/car/detail-"):
        return None
    clean_path = parsed.path.rstrip("/") or parsed.path
    return f"{BAMA_DOMAIN.rstrip('/')}{clean_path}"


def _extract_detail_urls_from_html(html: str) -> list[str]:
    """Collect listing URLs from anchors and embedded payloads."""
    found: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a", href=True):
        norm = _normalize_candidate_url(tag["href"])
        if norm:
            found.append(norm)
    for m in _DETAIL_PATH_RE.findall(html):
        norm = _normalize_candidate_url(m)
        if norm:
            found.append(norm)
    seen_local: set[str] = set()
    ordered: list[str] = []
    for u in found:
        if u not in seen_local:
            seen_local.add(u)
            ordered.append(u)
    return ordered


def _fetch_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    response = requests.get(url.strip(), headers=_random_headers(), timeout=timeout_seconds)
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _redis_client() -> redis.Redis:
    broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(broker, decode_responses=True)


def discover_and_enqueue_bama_urls() -> dict[str, object]:
    """
    Fetch configured list pages, extract /car/detail-* URLs, enqueue unseen URLs only.

    Dedup uses Redis SET ``seenad:bama:url_seen`` (override via SEENAD_BAMA_URL_SEEN_SET_KEY).
    Queue uses LPOP consumption consistent with ingestion (RPUSH producer side).
    """
    listing_sources = _listing_urls_to_fetch()
    candidates: list[str] = []
    for src in listing_sources:
        try:
            html = _fetch_listing_html(src)
            batch = _extract_detail_urls_from_html(html)
            LOGGER.info("bama_discovery fetched %s links=%s bytes=%s", src, len(batch), len(html))
            candidates.extend(batch)
        except requests.RequestException:
            LOGGER.warning("bama_discovery fetch failed url=%s", src, exc_info=True)

    queue_key = _queue_key()
    seen_key = _seen_key()

    unique_ordered: list[str] = []
    local_seen: set[str] = set()
    for u in candidates:
        if u not in local_seen:
            local_seen.add(u)
            unique_ordered.append(u)

    max_enqueue = int(os.getenv("BAMA_DISCOVERY_MAX_URLS_PER_RUN", "2000"))

    try:
        client = _redis_client()
    except redis.RedisError:
        LOGGER.exception("bama_discovery Redis connection failed.")
        return {
            "status": "redis_error",
            "listing_sources": listing_sources,
            "candidates_found": len(unique_ordered),
            "new_enqueued": 0,
            "queue_key": queue_key,
            "seen_key": seen_key,
        }

    enqueued = 0
    try:
        for url in unique_ordered:
            if enqueued >= max_enqueue:
                break
            try:
                if client.sadd(seen_key, url):
                    client.rpush(queue_key, url)
                    enqueued += 1
            except redis.RedisError:
                LOGGER.warning("bama_discovery Redis SADD/RPUSH failed for url=%s", url, exc_info=True)
    finally:
        try:
            client.close()
        except Exception:
            pass

    LOGGER.info(
        "bama_discovery done queue=%s seen_set=%s candidates=%s new_enqueued=%s",
        queue_key,
        seen_key,
        len(unique_ordered),
        enqueued,
    )

    return {
        "status": "ok",
        "listing_sources": listing_sources,
        "candidates_found": len(unique_ordered),
        "new_enqueued": enqueued,
        "queue_key": queue_key,
        "seen_key": seen_key,
    }


@celery_app.task(name="tasks.bama_discovery")
def bama_discovery_task() -> dict[str, object]:
    """Enqueue newly discovered Bama detail URLs into Redis for ``tasks.bama_ingestion_tick``."""
    return discover_and_enqueue_bama_urls()
