from __future__ import annotations

import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base, SessionLocal, engine
from app.models import CarEntity

LOGGER = logging.getLogger("bama-car-reference-sync-v2")
PRICE_URL = "https://bama.ir/price"
SITEMAP_PRICE_DETAIL_URL = "https://bama.ir/sitemap/price-detail"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _request_text(session: requests.Session, url: str) -> str:
    response = session.get(
        url,
        timeout=60,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://bama.ir/",
        },
    )
    response.raise_for_status()
    return response.text


def _resolve_ref(table: list[Any], value: Any) -> Any:
    if isinstance(value, int) and 0 <= value < len(table):
        return table[value]
    return value


def _extract_name_text(table: list[Any], value: Any) -> tuple[str, str]:
    node = _resolve_ref(table, value)
    if isinstance(node, dict):
        name = _resolve_ref(table, node.get("name"))
        text = _resolve_ref(table, node.get("text"))
        return _normalize_text(str(name or "")), _normalize_text(str(text or ""))
    if isinstance(node, str):
        normalized = _normalize_text(node)
        return normalized, normalized
    return "", ""


def _extract_sitemap_price_detail_urls(session: requests.Session) -> list[str]:
    xml_text = _request_text(session, SITEMAP_PRICE_DETAIL_URL)
    urls = re.findall(r"<loc>(https://bama\.ir/price/[^<]+)</loc>", xml_text)
    deduped = sorted(set(urls))
    return deduped


def _extract_nuxt_table_from_payload_json(payload_json: str) -> list[Any]:
    parsed = json.loads(payload_json)
    if not isinstance(parsed, list):
        raise RuntimeError("Invalid Nuxt payload format for price detail page.")
    return parsed


def _extract_reference_from_price_detail_payload(table: list[Any]) -> tuple[str, str, str] | None:
    for row in table:
        if not isinstance(row, dict):
            continue
        if not {"title", "seo", "brand", "model", "trim"}.issubset(row.keys()):
            continue
        brand_name, brand_text = _extract_name_text(table, row.get("brand"))
        model_name, model_text = _extract_name_text(table, row.get("model"))
        trim_name, trim_text = _extract_name_text(table, row.get("trim"))
        brand = brand_text or brand_name
        model = model_text or model_name
        trim = trim_text or trim_name
        if brand and model and trim:
            return brand, model, trim
    return None


def _replace_reference_table(rows: set[tuple[str, str, str]]) -> int:
    records: list[CarEntity] = []
    for brand, model, trim in sorted(rows):
        records.append(
            CarEntity(
                brand=brand,
                model=model,
                trim=trim,
                production_year_start=None,
                production_year_end=None,
                source_url=PRICE_URL,
            )
        )

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM car_entities"))
        db.execute(text("ALTER SEQUENCE car_entities_id_seq RESTART WITH 1"))
        for row in records:
            db.add(row)
        db.commit()
        return len(records)
    finally:
        db.close()


def _count_rows() -> int:
    db = SessionLocal()
    try:
        return db.scalar(select(func.count(CarEntity.id))) or 0
    finally:
        db.close()


def main() -> None:
    _configure_logging()
    Base.metadata.create_all(bind=engine, tables=[CarEntity.__table__])
    session = requests.Session()
    session.verify = False
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    detail_urls = _extract_sitemap_price_detail_urls(session)
    LOGGER.info("Found %s unique URLs in price-detail sitemap.", len(detail_urls))

    extracted_rows: set[tuple[str, str, str]] = set()
    for idx, detail_url in enumerate(detail_urls, start=1):
        payload_url = f"{detail_url.rstrip('/')}/_payload.json"
        try:
            payload_text = _request_text(session, payload_url)
            table = _extract_nuxt_table_from_payload_json(payload_text)
            row = _extract_reference_from_price_detail_payload(table)
            if row:
                extracted_rows.add(row)
        except Exception as exc:
            LOGGER.warning("Failed to parse detail payload: %s | %s", payload_url, exc)
        if idx % 25 == 0:
            LOGGER.info("Processed detail URLs: %s/%s | unique references: %s", idx, len(detail_urls), len(extracted_rows))
        time.sleep(random.uniform(0.35, 0.9))

    brand_count = len({brand for brand, _, _ in extracted_rows})
    model_count = len({(brand, model) for brand, model, _ in extracted_rows})
    trim_count = len(extracted_rows)

    LOGGER.info("Extracted official taxonomy: brands=%s models=%s trims=%s", brand_count, model_count, trim_count)
    inserted = _replace_reference_table(extracted_rows)
    final_count = _count_rows()

    LOGGER.info("car_entities refreshed from bama price taxonomy. inserted=%s final_count=%s", inserted, final_count)
    print(f"Reference sync v2 completed: brands={brand_count} models={model_count} trims={final_count}")


if __name__ == "__main__":
    main()
