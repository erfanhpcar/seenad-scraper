"""Daily zero-price benchmark run (writes benchmark_observation rows)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.zero_price_benchmark.config import assert_legacy_benchmark_writer_may_run, daily_limit
from src.zero_price_benchmark.hm_prices import fetch_price_from_catalog, load_catalog_index
from src.zero_price_benchmark.k45_prices import fetch_estimated_price, _session as k45_session
from src.zero_price_benchmark.seenad_prices import fetch_seenad_price

LOGGER = logging.getLogger(__name__)

SOURCES: tuple[str, ...] = ("SEENAD", "KHODRO45", "HAMRAH_MECHANIC")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_active_cars(session: Session, limit: int | None) -> list[Mapping[str, Any]]:
    sql = text(
        """
        SELECT id AS benchmark_car_id, seenad_car_id, mapping_status
        FROM benchmark_car
        WHERE is_active IS TRUE
        ORDER BY COALESCE(priority, 999999), id
        """
    )
    rows = session.execute(sql).mappings().all()
    if limit is not None:
        rows = rows[:limit]
    return list(rows)


def _load_mapping(session: Session, benchmark_car_id: int, source: str) -> Mapping[str, Any] | None:
    sql = text(
        """
        SELECT id, external_payload, status
        FROM benchmark_source_mapping
        WHERE benchmark_car_id = :car_id AND source = :source
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = session.execute(sql, {"car_id": benchmark_car_id, "source": source}).mappings().first()
    return row


def _insert_run(session: Session, expected: int) -> int:
    sql = text(
        """
        INSERT INTO benchmark_run (status, expected_count, processed_count, started_at, trigger)
        VALUES ('running', :expected, 0, :started_at, 'celery')
        RETURNING id
        """
    )
    run_id = session.execute(sql, {"expected": expected, "started_at": _utc_now()}).scalar_one()
    session.commit()
    return int(run_id)


def _finish_run(session: Session, run_id: int, processed: int, status: str = "completed") -> None:
    sql = text(
        """
        UPDATE benchmark_run
        SET processed_count = :processed, status = :status, finished_at = :finished_at
        WHERE id = :run_id
        """
    )
    session.execute(
        sql,
        {"processed": processed, "status": status, "finished_at": _utc_now(), "run_id": run_id},
    )
    session.commit()


def _insert_observation(
    session: Session,
    *,
    run_id: int,
    benchmark_car_id: int,
    source: str,
    status: str,
    price_irr: int | None,
    message: str | None,
    raw: Mapping[str, Any] | None,
) -> None:
    sql = text(
        """
        INSERT INTO benchmark_observation (
            run_id, benchmark_car_id, source, status, price_irr, error_message, raw_payload, fetched_at
        )
        VALUES (
            :run_id, :benchmark_car_id, :source, :status, :price_irr, :message,
            CAST(:raw AS jsonb), :fetched_at
        )
        """
    )
    session.execute(
        sql,
        {
            "run_id": run_id,
            "benchmark_car_id": benchmark_car_id,
            "source": source,
            "status": status,
            "price_irr": price_irr,
            "message": message,
            "raw": json.dumps(raw, ensure_ascii=False) if raw is not None else None,
            "fetched_at": _utc_now(),
        },
    )


def run_daily_benchmark(session: Session) -> dict[str, Any]:
    assert_legacy_benchmark_writer_may_run()
    limit = daily_limit()
    cars = _load_active_cars(session, limit)
    if not cars:
        return {"ok": False, "error": "no active benchmark_car rows"}

    run_id = _insert_run(session, expected=len(cars))
    hm_catalog = load_catalog_index()
    k45_http = k45_session()
    seenad_http = requests.Session()

    processed = 0
    stats: dict[str, dict[str, int]] = {s: {} for s in SOURCES}

    for car in cars:
        benchmark_car_id = int(car["benchmark_car_id"])
        seenad_car_id = int(car["seenad_car_id"])
        mapping_status = str(car.get("mapping_status") or "")

        for source in SOURCES:
            mapping = _load_mapping(session, benchmark_car_id, source) if source != "SEENAD" else None

            if source == "SEENAD":
                result = fetch_seenad_price(seenad_car_id=seenad_car_id, session=seenad_http)
                status = "OK" if result.ok else "SOURCE_ERROR"
                _insert_observation(
                    session,
                    run_id=run_id,
                    benchmark_car_id=benchmark_car_id,
                    source=source,
                    status=status,
                    price_irr=result.price,
                    message=result.message if not result.ok else None,
                    raw=result.raw,
                )
                stats[source][status] = stats[source].get(status, 0) + 1
                continue

            payload = mapping.get("external_payload") if mapping else None
            if isinstance(payload, str):
                payload = json.loads(payload)

            if source == "KHODRO45":
                if not isinstance(payload, dict):
                    _insert_observation(
                        session,
                        run_id=run_id,
                        benchmark_car_id=benchmark_car_id,
                        source=source,
                        status="NEEDS_MAPPING",
                        price_irr=None,
                        message="invalid K45 mapping payload",
                        raw=None,
                    )
                    stats[source]["NEEDS_MAPPING"] = stats[source].get("NEEDS_MAPPING", 0) + 1
                    continue
                k45 = fetch_estimated_price(k45_http, mapping_payload=payload)
                status = "OK" if k45.ok else "SOURCE_ERROR"
                _insert_observation(
                    session,
                    run_id=run_id,
                    benchmark_car_id=benchmark_car_id,
                    source=source,
                    status=status,
                    price_irr=k45.price,
                    message=k45.message if not k45.ok else None,
                    raw=k45.raw,
                )
                stats[source][status] = stats[source].get(status, 0) + 1
                continue

            if source == "HAMRAH_MECHANIC":
                if not isinstance(payload, dict):
                    _insert_observation(
                        session,
                        run_id=run_id,
                        benchmark_car_id=benchmark_car_id,
                        source=source,
                        status="NEEDS_MAPPING",
                        price_irr=None,
                        message="invalid HM mapping payload",
                        raw=None,
                    )
                    stats[source]["NEEDS_MAPPING"] = stats[source].get("NEEDS_MAPPING", 0) + 1
                    continue
                car_type_id = payload.get("car_type_id")
                car_year = payload.get("car_year")
                if car_type_id is None or car_year is None:
                    _insert_observation(
                        session,
                        run_id=run_id,
                        benchmark_car_id=benchmark_car_id,
                        source=source,
                        status="NEEDS_MAPPING",
                        price_irr=None,
                        message="HM mapping missing car_type_id/car_year",
                        raw=payload,
                    )
                    stats[source]["NEEDS_MAPPING"] = stats[source].get("NEEDS_MAPPING", 0) + 1
                    continue
                hm = fetch_price_from_catalog(
                    car_type_id=int(car_type_id),
                    car_year=int(car_year),
                    catalog=hm_catalog,
                )
                status = "OK" if hm.ok else "SOURCE_ERROR"
                _insert_observation(
                    session,
                    run_id=run_id,
                    benchmark_car_id=benchmark_car_id,
                    source=source,
                    status=status,
                    price_irr=hm.price,
                    message=hm.message if not hm.ok else None,
                    raw=hm.raw,
                )
                stats[source][status] = stats[source].get(status, 0) + 1

        processed += 1
        if processed % 25 == 0:
            session.commit()
            LOGGER.info("benchmark progress run_id=%s processed=%s/%s", run_id, processed, len(cars))

    session.commit()
    _finish_run(session, run_id, processed)
    return {
        "ok": True,
        "run_id": run_id,
        "expected_count": len(cars),
        "processed_count": processed,
        "stats_by_source": stats,
    }
