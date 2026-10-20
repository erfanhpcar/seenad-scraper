"""Khodro45 zero-price estimate via public carprice API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import requests

LOGGER = logging.getLogger(__name__)

TRIMYEARS_URL = "https://khodro45.com/api/v1/trimyears/"
ESTIMATE_URL = "https://khodro45.com/api/v1/carprice/public/estimated_price/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class K45PriceResult:
    ok: bool
    price: int | None
    message: str
    trimyear_id: int | None = None
    raw: Mapping[str, Any] | None = None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def resolve_trimyear_id(session: requests.Session, *, trim_id: int, year_id: int) -> int | None:
    response = session.get(TRIMYEARS_URL, params={"trim_id": trim_id, "year_id": year_id}, timeout=45)
    if response.status_code != 200:
        LOGGER.warning("k45 trimyears HTTP %s trim_id=%s year_id=%s", response.status_code, trim_id, year_id)
        return None
    payload = response.json()
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict) and first.get("id") is not None:
            return int(first["id"])
    return None


def fetch_estimated_price(
    session: requests.Session,
    *,
    mapping_payload: Mapping[str, Any],
) -> K45PriceResult:
    trimyear_id = mapping_payload.get("trimyear_id")
    trim_id = mapping_payload.get("trim_id")
    year_id = mapping_payload.get("year_id")

    if trimyear_id is None and trim_id is not None and year_id is not None:
        trimyear_id = resolve_trimyear_id(session, trim_id=int(trim_id), year_id=int(year_id))

    if trimyear_id is None:
        return K45PriceResult(ok=False, price=None, message="missing trimyear_id (and trimyears lookup failed)")

    body = {"source": "carprice", "trimyear_id": int(trimyear_id), "defects": []}
    response = session.post(ESTIMATE_URL, json=body, timeout=45)
    if response.status_code != 200:
        return K45PriceResult(
            ok=False,
            price=None,
            message=f"K45 estimate HTTP {response.status_code}",
            trimyear_id=int(trimyear_id),
        )
    data = response.json()
    price = data.get("price") or data.get("estimated_price") or data.get("market_price")
    if price is None and isinstance(data.get("data"), dict):
        nested = data["data"]
        price = nested.get("price") or nested.get("estimated_price")
    if price is None:
        return K45PriceResult(
            ok=False,
            price=None,
            message="K45 response has no price field",
            trimyear_id=int(trimyear_id),
            raw=data if isinstance(data, dict) else None,
        )
    return K45PriceResult(ok=True, price=int(price), message="ok", trimyear_id=int(trimyear_id), raw=data)
