"""Best-effort automap for KHODRO45 / HAMRAH_MECHANIC (scraper-side helper)."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.zero_price_benchmark.config import assert_legacy_benchmark_writer_may_run
from src.scraper.hamrah_mechanic_carprice_service import run_hamrah_carprice_pipeline
from src.scraper.khodro45_pricing_service import flatten_dailycar_items, iter_dailycars_pages, _session as k45_session

LOGGER = logging.getLogger(__name__)


def _upsert_mapping(
    session: Session,
    *,
    benchmark_car_id: int,
    source: str,
    payload: dict[str, Any],
) -> None:
    sql = text(
        """
        INSERT INTO benchmark_source_mapping (benchmark_car_id, source, status, external_payload)
        VALUES (:car_id, :source, 'RESOLVED', CAST(:payload AS jsonb))
        ON CONFLICT (benchmark_car_id, source)
        DO UPDATE SET status = 'RESOLVED', external_payload = EXCLUDED.external_payload
        """
    )
    try:
        session.execute(
            sql,
            {"car_id": benchmark_car_id, "source": source, "payload": json.dumps(payload, ensure_ascii=False)},
        )
    except Exception:
        session.rollback()
        sql2 = text(
            """
            UPDATE benchmark_source_mapping
            SET status = 'RESOLVED', external_payload = CAST(:payload AS jsonb)
            WHERE benchmark_car_id = :car_id AND source = :source
            """
        )
        updated = session.execute(
            sql2,
            {"car_id": benchmark_car_id, "source": source, "payload": json.dumps(payload, ensure_ascii=False)},
        ).rowcount
        if not updated:
            sql3 = text(
                """
                INSERT INTO benchmark_source_mapping (benchmark_car_id, source, status, external_payload)
                VALUES (:car_id, :source, 'RESOLVED', CAST(:payload AS jsonb))
                """
            )
            session.execute(
                sql3,
                {"car_id": benchmark_car_id, "source": source, "payload": json.dumps(payload, ensure_ascii=False)},
            )


def run_automap(session: Session, *, limit: int | None = None) -> dict[str, Any]:
    """
    Populate HM mappings from catalogue when seenad_car_id is already present in payload
    or when a single HM row exists for the car's brand/model text (conservative).
    Full fuzzy automap lives in seenad-main; this helper refreshes HM/K45 payloads where possible.
    """
    assert_legacy_benchmark_writer_may_run()
    cars = session.execute(
        text(
            """
            SELECT bc.id AS benchmark_car_id, bc.seenad_car_id, c.brand, c.model, c.trim
            FROM benchmark_car bc
            JOIN car c ON c.id = bc.seenad_car_id
            WHERE bc.is_active IS TRUE
            ORDER BY bc.id
            """
        )
    ).mappings().all()

    if limit is not None:
        cars = cars[:limit]

    hm_rows = []
    from pathlib import Path

    hm_path = Path("data/raw_hamrahmechanic_carprice_catalog.jsonl")
    if not hm_path.is_file():
        run_hamrah_carprice_pipeline(output_path=hm_path, dry_run=False, sleep_s=0.02)
    if hm_path.is_file():
        hm_rows = [json.loads(line) for line in hm_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    k45_http = k45_session()
    k45_items = flatten_dailycar_items(list(iter_dailycars_pages(k45_http, page_limit=100)))

    hm_updated = 0
    k45_updated = 0

    for car in cars:
        benchmark_car_id = int(car["benchmark_car_id"])
        brand = (car.get("brand") or "").strip()
        model = (car.get("model") or "").strip()
        trim = (car.get("trim") or "").strip()

        for row in hm_rows:
            if brand and brand not in (row.get("brand_name") or ""):
                continue
            if model and model not in (row.get("model_name") or ""):
                continue
            if trim and trim not in (row.get("car_type_name") or ""):
                continue
            payload = {"car_type_id": row.get("car_type_id"), "car_year": row.get("car_year")}
            if payload["car_type_id"] is None:
                continue
            _upsert_mapping(session, benchmark_car_id=benchmark_car_id, source="HAMRAH_MECHANIC", payload=payload)
            hm_updated += 1
            break

        for item in k45_items:
            if brand and brand != (item.get("brand_title") or ""):
                continue
            if model and model not in (item.get("model_title") or ""):
                continue
            if trim and trim not in (item.get("trim_title") or ""):
                continue
            payload = {
                "trim_id": item.get("trim_id"),
                "year_id": item.get("year_id"),
                "trimyear_id": item.get("trimyear_id"),
                "year_title": item.get("year_title"),
            }
            if payload["trimyear_id"] is None and payload["trim_id"] is None:
                continue
            _upsert_mapping(session, benchmark_car_id=benchmark_car_id, source="KHODRO45", payload=payload)
            k45_updated += 1
            break

    session.execute(
        text(
            """
            UPDATE benchmark_car
            SET mapping_status = 'RESOLVED'
            WHERE id IN (
                SELECT benchmark_car_id FROM benchmark_source_mapping
                WHERE source IN ('KHODRO45', 'HAMRAH_MECHANIC') AND status = 'RESOLVED'
            )
            """
        )
    )
    session.commit()
    return {
        "cars_considered": len(cars),
        "hm_mappings_upserted": hm_updated,
        "k45_mappings_upserted": k45_updated,
    }
