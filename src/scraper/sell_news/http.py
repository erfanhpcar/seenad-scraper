"""HTTP session with explicit timeouts, retries, and a real browser UA."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.scraper.sell_news.constants import (
    CONNECT_TIMEOUT_S,
    DELAY_MAX_S,
    DELAY_MIN_S,
    MAX_RETRIES,
    READ_TIMEOUT_S,
    USER_AGENT,
)

LOGGER = logging.getLogger(__name__)


class SellNewsHttp:
    def __init__(
        self,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        read_timeout: float = READ_TIMEOUT_S,
        max_retries: int = MAX_RETRIES,
        delay_min: float = DELAY_MIN_S,
        delay_max: float = DELAY_MAX_S,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.session = requests.Session()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
        }
        if extra_headers:
            headers.update(extra_headers)
        self.session.headers.update(headers)
        retry = Retry(
            total=max(0, max_retries),
            connect=max(0, max_retries),
            read=max(0, max_retries),
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def sleep_polite(self) -> None:
        low = min(self.delay_min, self.delay_max)
        high = max(self.delay_min, self.delay_max)
        if high <= 0:
            return
        time.sleep(random.uniform(low, high))

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", (self.connect_timeout, self.read_timeout))
        LOGGER.debug("GET %s", url)
        return self.session.get(url, timeout=timeout, **kwargs)
