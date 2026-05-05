#!/usr/bin/env python3
"""
Crawl Bama calculator taxonomy via official CAD APIs (same host axios uses: https://bama.ir).

Endpoints (discovered from Nuxt bundles + Network parity checks):
  GET /cad/api/BasicInfo/brands
  GET /cad/api/BasicInfo/models?brandId=<int>
  GET /cad/api/BasicInfo/years?BrandId=<int>&ModelId=<int>
  GET /cad/api/BasicInfo/trims?BrandId=<int>&ModelId=<int>&ModelYear=<int Jalali>

Persian calendar years from ``years`` / ``ModelYear`` are converted to Gregorian calendar years
for ``production_year_start`` / ``production_year_end`` (calendar year of Farvardin 1 … last day of Esfand).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import jdatetime
import requests
from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.models import CarEntity  # noqa: E402

LOGGER = logging.getLogger("crawl-bama-reference")

BAMA_ORIGIN = "https://bama.ir"
BRANDS_PATH = "/cad/api/BasicInfo/brands"
MODELS_PATH = "/cad/api/BasicInfo/models"
YEARS_PATH = "/cad/api/BasicInfo/years"
TRIMS_PATH = "/cad/api/BasicInfo/trims"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://bama.ir/calculator",
    "Origin": "https://bama.ir",
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def _normalize_label(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _jalali_bounds_to_gregorian_year_span(j_min: int, j_max: int) -> tuple[int, int]:
    """Map inclusive Jalali production-year span to Gregorian calendar-year endpoints."""
    start_g = jdatetime.date(j_min, 1, 1).togregorian()
    try:
        end_j = jdatetime.date(j_max, 12, 30)
    except ValueError:
        end_j = jdatetime.date(j_max, 12, 29)
    end_g = end_j.togregorian()
    return start_g.year, end_g.year


class BamaCadClient:
    def __init__(self, session: requests.Session, delay_sec: float) -> None:
        self._session = session
        self._delay_sec = delay_sec

    def _sleep(self) -> None:
        if self._delay_sec > 0:
            time.sleep(self._delay_sec)

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        empty_on_http: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        url = f"{BAMA_ORIGIN}{path}"
        self._sleep()
        response = self._session.get(url, params=params or {}, headers=HEADERS, timeout=90)
        if response.status_code in empty_on_http:
            LOGGER.warning(
                "HTTP %s for %s params=%s — treating as empty data",
                response.status_code,
                path,
                params or {},
            )
            return {"status": True, "data": []}
        response.raise_for_status()
        payload = response.json()
        if not payload.get("status"):
            errs = payload.get("errors") or []
            raise RuntimeError(f"Bama API error on {path}: {errs}")
        return payload

    def brands(self) -> list[dict[str, Any]]:
        data = self._get(BRANDS_PATH)["data"]
        return data if isinstance(data, list) else []

    def models(self, brand_id: int) -> list[dict[str, Any]]:
        data = self._get(MODELS_PATH, {"brandId": brand_id})["data"]
        return data if isinstance(data, list) else []

    def years(self, brand_id: int, model_id: int) -> list[int]:
        data = self._get(YEARS_PATH, {"BrandId": brand_id, "ModelId": model_id}, empty_on_http=(404,))[
            "data"
        ]
        if not isinstance(data, list):
            return []
        out: list[int] = []
        for row in data:
            if isinstance(row, dict) and row.get("value") is not None:
                try:
                    out.append(int(row["value"]))
                except (TypeError, ValueError):
                    continue
        return out

    def trims(self, brand_id: int, model_id: int, model_year_jalali: int) -> list[dict[str, Any]]:
        data = self._get(
            TRIMS_PATH,
            {"BrandId": brand_id, "ModelId": model_id, "ModelYear": model_year_jalali},
            empty_on_http=(404,),
        )["data"]
        return data if isinstance(data, list) else []


def crawl_tree(client: BamaCadClient) -> dict[tuple[str, str, str], set[int]]:
    """
    Returns map (brand_label, model_label, trim_label) -> set of Jalali model years where trim appears.
    """
    trim_jalali_years: defaultdict[tuple[str, str, str], set[int]] = defaultdict(set)

    brands = client.brands()
    LOGGER.info("Brands fetched: %s", len(brands))

    for bi, brand in enumerate(brands, start=1):
        brand_id = int(brand["value"])
        brand_label = _normalize_label(str(brand.get("text") or ""))
        if not brand_label:
            LOGGER.warning("Skip brand id=%s (empty label)", brand_id)
            continue

        models = client.models(brand_id)
        LOGGER.info("[%s/%s] %s models=%s", bi, len(brands), brand_label, len(models))

        for model in models:
            model_id = int(model["value"])
            model_label = _normalize_label(str(model.get("text") or ""))
            if not model_label:
                continue

            years = client.years(brand_id, model_id)
            if not years:
                continue

            for jy in years:
                trims = client.trims(brand_id, model_id, jy)
                for tr in trims:
                    trim_label = _normalize_label(str(tr.get("text") or ""))
                    if not trim_label:
                        trim_label = _normalize_label(str(tr.get("name") or ""))
                    if not trim_label:
                        continue
                    trim_jalali_years[(brand_label, model_label, trim_label)].add(jy)

    return trim_jalali_years


def replace_entities(trim_jalali_years: dict[tuple[str, str, str], set[int]], dry_run: bool) -> tuple[int, int, int]:
    rows: list[CarEntity] = []
    for (brand, model, trim_label), jyears in sorted(trim_jalali_years.items()):
        if not jyears:
            continue
        j_min, j_max = min(jyears), max(jyears)
        gy_start, gy_end = _jalali_bounds_to_gregorian_year_span(j_min, j_max)
        rows.append(
            CarEntity(
                brand=brand[:100],
                model=model[:120],
                trim=trim_label[:160],
                production_year_start=gy_start,
                production_year_end=gy_end,
                source_url="https://bama.ir/calculator",
            )
        )

    brands_n = len({b for b, _, _ in trim_jalali_years})
    models_n = len({(b, m) for b, m, _ in trim_jalali_years})
    trims_n = len(trim_jalali_years)

    if dry_run:
        LOGGER.info("Dry-run: would insert %s trims (%s brands / %s models)", trims_n, brands_n, models_n)
        return brands_n, models_n, trims_n

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM car_entities"))
        db.execute(text("ALTER SEQUENCE car_entities_id_seq RESTART WITH 1"))
        for row in rows:
            db.add(row)
        db.commit()
        final = db.scalar(select(func.count(CarEntity.id))) or 0
        LOGGER.info("Inserted rows=%s verified_count=%s", len(rows), final)
    finally:
        db.close()

    return brands_n, models_n, trims_n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.08, help="Sleep between HTTP calls (seconds).")
    parser.add_argument("--dry-run", action="store_true", help="Crawl only; do not touch the database.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    _configure_logging(args.verbose)

    session = requests.Session()
    session.verify = True

    client = BamaCadClient(session=session, delay_sec=args.delay)
    trim_map = crawl_tree(client)
    brands_n, models_n, trims_n = replace_entities(trim_map, dry_run=args.dry_run)

    print(
        f"Summary — distinct brands (labels): {brands_n}, "
        f"distinct models (brand+model): {models_n}, "
        f"trims (DB rows): {trims_n}"
    )


if __name__ == "__main__":
    main()
