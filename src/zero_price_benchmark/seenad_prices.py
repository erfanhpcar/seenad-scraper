"""SeenAd zero-km price for benchmark (optional HTTP to main app)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from src.zero_price_benchmark.config import seenad_valuation_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SeenadPriceResult:
    ok: bool
    price: int | None
    message: str
    raw: Mapping[str, Any] | None = None


def fetch_seenad_price(*, seenad_car_id: int, session: requests.Session | None = None) -> SeenadPriceResult:
    try:
        from src.zero_price_benchmark.seenad_valuation_local import evaluate_zero_price  # type: ignore

        return evaluate_zero_price(seenad_car_id)
    except ImportError:
        pass

    url = seenad_valuation_url()
    if not url:
        return SeenadPriceResult(
            ok=False,
            price=None,
            message="SEENAD_BENCHMARK_VALUATION_URL not set and local CarValuationService missing",
        )

    http = session or requests.Session()
    formatted = url.replace("{car_id}", str(seenad_car_id))
    try:
        response = http.post(formatted, json={"car_id": seenad_car_id, "mileage": 0}, timeout=60)
    except requests.RequestException as exc:
        return SeenadPriceResult(ok=False, price=None, message=f"SeenAd valuation request failed: {exc}")

    if response.status_code != 200:
        return SeenadPriceResult(
            ok=False,
            price=None,
            message=f"SeenAd valuation HTTP {response.status_code}",
        )
    data = response.json()
    price = None
    if isinstance(data, dict):
        price = data.get("estimated_price") or data.get("price")
        if price is None and isinstance(data.get("data"), dict):
            price = data["data"].get("estimated_price") or data["data"].get("price")
    if price is None:
        return SeenadPriceResult(ok=False, price=None, message="SeenAd valuation response missing price", raw=data)
    return SeenadPriceResult(ok=True, price=int(price), message="ok", raw=data if isinstance(data, dict) else None)
