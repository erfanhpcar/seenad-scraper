"""Hamrah Mechanic prices via public carprice catalogue (SSR), not raw-price API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.scraper.hamrah_mechanic_carprice_service import run_hamrah_carprice_pipeline


@dataclass(frozen=True, slots=True)
class HmPriceResult:
    ok: bool
    price: int | None
    message: str
    raw: dict[str, Any] | None = None


def build_catalog_index(rows: list[dict[str, Any]]) -> dict[tuple[int | None, int | None], dict[str, Any]]:
    index: dict[tuple[int | None, int | None], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("car_type_id"), row.get("car_year"))
        index[key] = row
    return index


def load_catalog_index() -> dict[tuple[int | None, int | None], dict[str, Any]]:
    from pathlib import Path
    import json

    path = Path("data/raw_hamrahmechanic_carprice_catalog.jsonl")
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return build_catalog_index(rows)

    summary = run_hamrah_carprice_pipeline(
        output_path=path,
        dry_run=False,
        sleep_s=0.02,
    )
    if summary.items_seen <= 0:
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return build_catalog_index(rows)


def fetch_price_from_catalog(
    *,
    car_type_id: int,
    car_year: int,
    catalog: dict[tuple[int | None, int | None], dict[str, Any]],
) -> HmPriceResult:
    row = catalog.get((car_type_id, car_year))
    if not row:
        return HmPriceResult(ok=False, price=None, message="car_type_id/car_year not in HM catalogue", raw=None)
    price = row.get("market_price")
    if price is None:
        return HmPriceResult(ok=False, price=None, message="HM catalogue row has no market_price", raw=row)
    return HmPriceResult(ok=True, price=int(price), message="ok", raw=row)
