"""Periodic discovery of listing detail URLs into Redis ingestion queues."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import redis
import requests
from bs4 import BeautifulSoup
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from app.celery_app import celery_app
from app.scrapers.bama_detail_parser import BAMA_DOMAIN, _random_headers
from app.scrapers.bama_scraper import USER_AGENTS

LOGGER = logging.getLogger(__name__)

_MODERN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

_DETAIL_PATH_RE = re.compile(r"/car/detail-[^\s\"'<>?#]+", re.IGNORECASE)
_KN_DETAIL_RE = re.compile(r"/used-cars/[0-9a-f-]{36}", re.IGNORECASE)
_HM_DETAIL_RE = re.compile(r"/cars-for-sale/[A-Za-z0-9%_-]+/[A-Za-z0-9%_-]+/\d+/?", re.IGNORECASE)


def _classify_request_error(exc: BaseException) -> str:
    """Return a short human-friendly tag for common request failures."""
    if isinstance(exc, requests.exceptions.SSLError):
        msg = str(exc).lower()
        if "handshake" in msg or "sslv3_alert" in msg:
            return "ssl_handshake_failure"
        return "ssl_error"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        msg = str(exc).lower()
        if "reset by peer" in msg or "connection reset" in msg:
            return "connection_reset_by_peer"
        if "name or service not known" in msg or "failed to resolve" in msg:
            return "dns_failure"
        return "connection_error"
    if isinstance(exc, requests.exceptions.HTTPError):
        return "http_error"
    return "request_error"


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
    response = requests.get(url.strip(), headers=_random_headers(url.strip()), timeout=timeout_seconds)
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _redis_client() -> redis.Redis:
    broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(broker, decode_responses=True)


def _fetch_html_generic(url: str, *, timeout_seconds: float = 45.0) -> str:
    """GET an HTML page using a modern UA and conservative HTTP/1.1 headers.

    ``timeout_seconds`` is a single value applied to both connect and read; we use a
    longer wall-clock here than for ingestion to be tolerant of slow Arvan edges.
    """
    headers = {
        "User-Agent": _MODERN_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Referer": url.strip(),
        "Upgrade-Insecure-Requests": "1",
    }
    # (connect, read) split so SSL handshake gets a generous window before read.
    response = requests.get(url.strip(), headers=headers, timeout=(15.0, timeout_seconds))
    response.raise_for_status()
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _fetch_hm_listing_html(url: str, *, timeout_seconds: float = 45.0) -> str:
    """Fetch a hamrah-mechanic list page, preferring the domain (resolved via /etc/hosts).

    Order of attempts is intentionally:
      1. Real domain over HTTPS (SNI works because /etc/hosts already routes to the IP).
      2. Direct IP over HTTP (Arvan edge serves HTTP fine; avoids the bad TLS handshake).
      3. Direct IP over HTTPS with verify=False (last-resort, server frequently rejects with
         ``SSLV3_ALERT_HANDSHAKE_FAILURE`` because it has no cert pinned to the bare IP).
    """
    try:
        return _fetch_html_generic(url, timeout_seconds=timeout_seconds)
    except requests.RequestException as exc:
        LOGGER.warning(
            "hm discovery domain fetch failed url=%s reason=%s detail=%s",
            url,
            _classify_request_error(exc),
            exc,
        )

    parsed = urlparse(url.strip())
    path_part = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, parsed.fragment))
    host = parsed.netloc or "www.hamrah-mechanic.com"
    ip_candidates = [
        os.getenv("HM_DIRECT_IP", "").strip() or "185.143.232.253",
        os.getenv("HM_DIRECT_IP_2", "").strip() or "185.143.235.253",
    ]
    common_headers = {
        "User-Agent": _MODERN_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Host": host,
        "Referer": "https://www.hamrah-mechanic.com/cars-for-sale/",
        "Upgrade-Insecure-Requests": "1",
    }
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
    for ip in ip_candidates:
        if not ip:
            continue
        for scheme, verify_ssl in (("http", True), ("https", False)):
            target = f"{scheme}://{ip}{path_part}"
            try:
                response = requests.get(
                    target,
                    headers=common_headers,
                    timeout=(15.0, timeout_seconds),
                    verify=verify_ssl,
                )
                response.raise_for_status()
                if scheme == "https":
                    LOGGER.warning(
                        "hm discovery succeeded via direct-IP HTTPS with verify=False ip=%s url=%s",
                        ip,
                        url,
                    )
                if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                    response.encoding = response.apparent_encoding or "utf-8"
                return response.text
            except requests.RequestException as exc:
                LOGGER.warning(
                    "hm discovery direct-ip fetch failed ip=%s scheme=%s reason=%s detail=%s",
                    ip,
                    scheme,
                    _classify_request_error(exc),
                    exc,
                )
                continue
    raise requests.RequestException(f"HM fetch failed for {url}")


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


def _kn_listing_pages_to_fetch() -> list[str]:
    """Return karnameh paginated listing URLs.

    Override list with ``KN_DISCOVERY_LIST_URLS`` (comma separated). Otherwise sweep pages
    1..``KN_DISCOVERY_PAGES_MAX`` (default 8) which currently covers the full public catalog
    (~156 unique listings as of 2026-05).
    """
    raw = os.getenv("KN_DISCOVERY_LIST_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    try:
        pages_max = int(os.getenv("KN_DISCOVERY_PAGES_MAX", "8"))
    except ValueError:
        pages_max = 8
    pages_max = max(1, min(pages_max, 30))
    urls = ["https://karnameh.com/buy-used-cars"]
    for page in range(2, pages_max + 1):
        urls.append(f"https://karnameh.com/buy-used-cars?page={page}")
    return urls


def _hm_listing_pages_to_fetch() -> list[str]:
    """Return hamrah-mechanic listing URLs (multiple filters/pages for breadth).

    Override list with ``HM_DISCOVERY_LIST_URLS``. Default sweeps the main feed plus a few
    high-yield filters and the first ``HM_DISCOVERY_PAGES_MAX`` (default 5) paginated views.
    """
    raw = os.getenv("HM_DISCOVERY_LIST_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    try:
        pages_max = int(os.getenv("HM_DISCOVERY_PAGES_MAX", "5"))
    except ValueError:
        pages_max = 5
    pages_max = max(1, min(pages_max, 20))
    urls = [
        "https://www.hamrah-mechanic.com/cars-for-sale/",
        "https://www.hamrah-mechanic.com/cars-for-sale/?kmStatus=2",
        "https://www.hamrah-mechanic.com/cars-for-sale/?sortBy=newest",
    ]
    for page in range(2, pages_max + 1):
        urls.append(f"https://www.hamrah-mechanic.com/cars-for-sale/?page={page}")
    return urls


def _normalize_kn_detail_url(raw_href: str) -> str | None:
    href = unquote(raw_href.strip())
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    full = urljoin("https://karnameh.com/", href)
    parsed = urlparse(full)
    if parsed.netloc not in {"karnameh.com", "www.karnameh.com"}:
        return None
    match = _KN_DETAIL_RE.search(unquote(parsed.path))
    if not match:
        return None
    return f"https://karnameh.com{match.group(0).rstrip('/')}"


def _normalize_hm_detail_url(raw_href: str) -> str | None:
    href = raw_href.strip()
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    full = urljoin("https://www.hamrah-mechanic.com/", href)
    parsed = urlparse(full)
    if "hamrah-mechanic.com" not in parsed.netloc:
        return None
    match = _HM_DETAIL_RE.search(parsed.path)
    if not match:
        # HM detail paths sometimes contain encoded spaces (e.g. "tiggo%207%20pro");
        # try once with the raw unquoted path for resilience.
        match = _HM_DETAIL_RE.search(unquote(parsed.path))
        if not match:
            return None
    path = match.group(0).rstrip("/") + "/"
    return f"https://www.hamrah-mechanic.com{path}"


def _extract_urls_from_html(
    html: str,
    *,
    normalizer: callable,
    embedded_re: re.Pattern[str],
) -> list[str]:
    found: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a", href=True):
        norm = normalizer(tag["href"])
        if norm:
            found.append(norm)
    for m in embedded_re.findall(html):
        norm = normalizer(m)
        if norm:
            found.append(norm)
    ordered: list[str] = []
    seen_local: set[str] = set()
    for url in found:
        if url not in seen_local:
            seen_local.add(url)
            ordered.append(url)
    return ordered


def _enqueue_unseen_urls(
    *,
    urls: list[str],
    queue_key: str,
    seen_key: str,
    max_enqueue: int,
) -> int:
    enqueued = 0
    client = _redis_client()
    try:
        for url in urls:
            if enqueued >= max_enqueue:
                break
            if client.sadd(seen_key, url):
                client.rpush(queue_key, url)
                enqueued += 1
    finally:
        try:
            client.close()
        except Exception:
            pass
    return enqueued


@celery_app.task(name="tasks.crawl_listing_pages")
def crawl_listing_pages_task() -> dict[str, object]:
    """Discover Bama/KN/HM detail URLs and enqueue unseen links for ingestion ticks."""
    bama_queue = _queue_key()
    bama_seen = _seen_key()
    kn_queue = os.getenv("SEENAD_KN_URL_QUEUE_KEY", "seenad:kn:url_queue").strip() or "seenad:kn:url_queue"
    hm_queue = os.getenv("SEENAD_HM_URL_QUEUE_KEY", "seenad:hm:url_queue").strip() or "seenad:hm:url_queue"
    kn_seen = os.getenv("SEENAD_KN_URL_SEEN_SET_KEY", "seenad:kn:url_seen").strip() or "seenad:kn:url_seen"
    hm_seen = os.getenv("SEENAD_HM_URL_SEEN_SET_KEY", "seenad:hm:url_seen").strip() or "seenad:hm:url_seen"
    max_bama = int(os.getenv("BAMA_DISCOVERY_MAX_URLS_PER_RUN", "2000"))
    max_kn = int(os.getenv("KN_DISCOVERY_MAX_URLS_PER_RUN", "200"))
    max_hm = int(os.getenv("HM_DISCOVERY_MAX_URLS_PER_RUN", "200"))

    bama_candidates: list[str] = []
    kn_candidates: list[str] = []
    hm_candidates: list[str] = []

    for src in _listing_urls_to_fetch():
        try:
            html = _fetch_listing_html(src)
            batch = _extract_detail_urls_from_html(html)
            LOGGER.info("crawl_listing_pages bama source=%s links=%s", src, len(batch))
            if not batch:
                LOGGER.warning("crawl_listing_pages bama: No URLs found in list page source=%s", src)
            bama_candidates.extend(batch)
        except requests.RequestException as exc:
            LOGGER.warning(
                "crawl_listing_pages Bama fetch failed source=%s reason=%s detail=%s",
                src,
                _classify_request_error(exc),
                exc,
            )

    for src in _kn_listing_pages_to_fetch():
        try:
            html = _fetch_html_generic(src)
            batch = _extract_urls_from_html(html, normalizer=_normalize_kn_detail_url, embedded_re=_KN_DETAIL_RE)
            LOGGER.info("crawl_listing_pages kn source=%s links=%s", src, len(batch))
            if not batch:
                LOGGER.warning("crawl_listing_pages kn: No URLs found in list page source=%s", src)
            kn_candidates.extend(batch)
        except requests.RequestException as exc:
            LOGGER.warning(
                "crawl_listing_pages KN fetch failed source=%s reason=%s detail=%s",
                src,
                _classify_request_error(exc),
                exc,
            )

    for src in _hm_listing_pages_to_fetch():
        try:
            html = _fetch_hm_listing_html(src)
            batch = _extract_urls_from_html(html, normalizer=_normalize_hm_detail_url, embedded_re=_HM_DETAIL_RE)
            LOGGER.info("crawl_listing_pages hm source=%s links=%s", src, len(batch))
            if not batch:
                LOGGER.warning("crawl_listing_pages hm: No URLs found in list page source=%s", src)
            hm_candidates.extend(batch)
        except requests.RequestException as exc:
            LOGGER.warning(
                "crawl_listing_pages HM fetch failed source=%s reason=%s detail=%s",
                src,
                _classify_request_error(exc),
                exc,
            )

    bama_unique = list(dict.fromkeys(bama_candidates))
    kn_unique = list(dict.fromkeys(kn_candidates))
    hm_unique = list(dict.fromkeys(hm_candidates))

    try:
        bama_enqueued = _enqueue_unseen_urls(
            urls=bama_unique,
            queue_key=bama_queue,
            seen_key=bama_seen,
            max_enqueue=max_bama,
        )
        kn_enqueued = _enqueue_unseen_urls(urls=kn_unique, queue_key=kn_queue, seen_key=kn_seen, max_enqueue=max_kn)
        hm_enqueued = _enqueue_unseen_urls(urls=hm_unique, queue_key=hm_queue, seen_key=hm_seen, max_enqueue=max_hm)
    except redis.RedisError:
        LOGGER.exception("crawl_listing_pages Redis error")
        return {
            "status": "redis_error",
            "bama_candidates_found": len(bama_unique),
            "kn_candidates_found": len(kn_unique),
            "hm_candidates_found": len(hm_unique),
            "bama_new_enqueued": 0,
            "kn_new_enqueued": 0,
            "hm_new_enqueued": 0,
        }

    LOGGER.info(
        "crawl_listing_pages done bama_candidates=%s kn_candidates=%s hm_candidates=%s bama_enqueued=%s kn_enqueued=%s hm_enqueued=%s",
        len(bama_unique),
        len(kn_unique),
        len(hm_unique),
        bama_enqueued,
        kn_enqueued,
        hm_enqueued,
    )
    return {
        "status": "ok",
        "bama_candidates_found": len(bama_unique),
        "kn_candidates_found": len(kn_unique),
        "hm_candidates_found": len(hm_unique),
        "bama_new_enqueued": bama_enqueued,
        "kn_new_enqueued": kn_enqueued,
        "hm_new_enqueued": hm_enqueued,
        "bama_queue_key": bama_queue,
        "kn_queue_key": kn_queue,
        "hm_queue_key": hm_queue,
    }
