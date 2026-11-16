"""PostgreSQL integration tests for latest-only EV charging snapshot persistence."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.repositories.ev_charging_snapshot_repository import (
    canonical_snapshot_bytes,
    load_latest_snapshot_bytes,
    sha256_hex,
    upsert_latest_snapshot,
)
from src.scraper import ev_charging_live as live

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "tests" / "sql" / "ev_charging_snapshot_integration_schema.sql"
OPT_IN_ENV = "EV_CHARGING_SNAPSHOT_PG_TESTS"
FORBIDDEN_DB = frozenset({"seenad_db", "postgres"})


def _opt_in() -> bool:
    return os.getenv(OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes"}


def _admin_url() -> str:
    url = os.getenv("EV_CHARGING_SNAPSHOT_TEST_ADMIN_URL", "").strip()
    if not url:
        raise RuntimeError(f"{OPT_IN_ENV}=1 requires EV_CHARGING_SNAPSHOT_TEST_ADMIN_URL")
    return url


@pytest.fixture(scope="module")
def isolated_engine() -> Engine:
    if not _opt_in():
        pytest.skip(f"{OPT_IN_ENV} not enabled")
    admin_engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    db_name = f"seenad_ev_charging_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    base = _admin_url()
    if "@" in base:
        prefix, suffix = base.rsplit("/", 1)
        test_url = f"{prefix}/{db_name}"
    else:
        test_url = f"{base}/{db_name}"

    engine = create_engine(test_url, pool_pre_ping=True)
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    statements = [
        chunk.strip()
        for chunk in "\n".join(
            line for line in schema.splitlines() if not line.strip().startswith("--")
        ).split(";")
        if chunk.strip()
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))

    yield engine

    engine.dispose()
    admin_engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
    admin_engine.dispose()


@pytest.fixture
def session_factory(isolated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=isolated_engine, autoflush=False, autocommit=False, class_=Session)


@pytest.fixture(autouse=True)
def _reset_ev_snapshot_table(isolated_engine: Engine) -> None:
    with isolated_engine.begin() as conn:
        conn.execute(text("DELETE FROM ev_charging_latest_snapshot"))


def _snapshot(fetched_at: str | None = None, stations: int = 1) -> dict:
    iso = fetched_at or datetime.now(UTC).isoformat()
    return {
        "schemaVersion": 1,
        "source": "emapna",
        "fetchedAt": iso,
        "counts": {"stations": stations, "devices": 1, "connectors": 1},
        "stations": [{
            "stationId": "st1",
            "name": "Station",
            "devices": [{
                "deviceId": "dv1",
                "stationId": "st1",
                "connectors": [{
                    "connectorId": "a",
                    "compositeKey": "dv1::a",
                    "statusCode": 1,
                    "status": "Available",
                }],
            }],
        }],
    }


def test_first_insert_and_sha_over_snapshot_bytes(session_factory: sessionmaker[Session]) -> None:
    snap = _snapshot()
    live.validate_snapshot(snap)
    raw = canonical_snapshot_bytes(snap)
    digest = sha256_hex(raw)
    session = session_factory()
    with session.begin():
        upsert_latest_snapshot(session, snap, raw, digest)
    session.close()

    session = session_factory()
    loaded = load_latest_snapshot_bytes(session)
    session.close()
    assert loaded is not None
    got_raw, got_digest = loaded
    assert got_digest == digest
    assert hashlib.sha256(got_raw).hexdigest() == digest
    assert got_raw == raw


def test_out_of_order_rejection(session_factory: sessionmaker[Session]) -> None:
    older = _snapshot(fetched_at="2026-01-01T00:00:00+00:00")
    newer = _snapshot(fetched_at="2026-01-02T00:00:00+00:00")
    session = session_factory()
    with session.begin():
        raw_new = canonical_snapshot_bytes(newer)
        upsert_latest_snapshot(session, newer, raw_new, sha256_hex(raw_new))
    session.close()

    session = session_factory()
    with pytest.raises(live.CrawlRejected, match="out-of-order"):
        with session.begin():
            raw_old = canonical_snapshot_bytes(older)
            upsert_latest_snapshot(session, older, raw_old, sha256_hex(raw_old))
    session.close()


def test_same_timestamp_different_sha_rejection(session_factory: sessionmaker[Session]) -> None:
    base_time = "2026-01-03T00:00:00+00:00"
    first = _snapshot(fetched_at=base_time)
    second = _snapshot(fetched_at=base_time, stations=2)
    second["stations"].append({
        "stationId": "st2",
        "name": "Station 2",
        "devices": [{
            "deviceId": "dv2",
            "stationId": "st2",
            "connectors": [{
                "connectorId": "b",
                "compositeKey": "dv2::b",
                "statusCode": 2,
                "status": "Charging",
            }],
        }],
    })
    second["counts"] = {"stations": 2, "devices": 2, "connectors": 2}
    live.validate_snapshot(second)

    session = session_factory()
    with session.begin():
        raw_first = canonical_snapshot_bytes(first)
        upsert_latest_snapshot(session, first, raw_first, sha256_hex(raw_first))
    session.close()

    session = session_factory()
    with pytest.raises(live.CrawlRejected, match="same timestamp"):
        with session.begin():
            raw_second = canonical_snapshot_bytes(second)
            upsert_latest_snapshot(session, second, raw_second, sha256_hex(raw_second))
    session.close()


def test_duplicate_sha_rejection(session_factory: sessionmaker[Session]) -> None:
    snap = _snapshot(fetched_at="2026-01-04T00:00:00+00:00")
    session = session_factory()
    with session.begin():
        raw = canonical_snapshot_bytes(snap)
        digest = sha256_hex(raw)
        upsert_latest_snapshot(session, snap, raw, digest)
    session.close()

    session = session_factory()
    with pytest.raises(live.CrawlRejected, match="duplicate snapshot sha256"):
        with session.begin():
            upsert_latest_snapshot(session, snap, raw, digest)
    session.close()


def test_coverage_decline_guard(session_factory: sessionmaker[Session]) -> None:
    bigger = _snapshot(fetched_at="2026-01-05T00:00:00+00:00", stations=10)
    bigger["counts"] = {"stations": 10, "devices": 10, "connectors": 10}
    smaller = _snapshot(fetched_at="2026-01-06T00:00:00+00:00", stations=1)

    session = session_factory()
    with session.begin():
        raw_big = canonical_snapshot_bytes(bigger)
        upsert_latest_snapshot(session, bigger, raw_big, sha256_hex(raw_big))
    session.close()

    session = session_factory()
    with pytest.raises(live.CrawlRejected, match="declined >10%"):
        with session.begin():
            raw_small = canonical_snapshot_bytes(smaller)
            upsert_latest_snapshot(session, smaller, raw_small, sha256_hex(raw_small))
    session.close()


def test_transaction_rollback_preserves_prior(session_factory: sessionmaker[Session]) -> None:
    first = _snapshot(fetched_at="2026-01-07T00:00:00+00:00")
    session = session_factory()
    with session.begin():
        raw = canonical_snapshot_bytes(first)
        upsert_latest_snapshot(session, first, raw, sha256_hex(raw))
    session.close()

    stale = _snapshot(fetched_at="2026-01-06T00:00:00+00:00")
    session = session_factory()
    try:
        with session.begin():
            upsert_latest_snapshot(session, stale, canonical_snapshot_bytes(stale), sha256_hex(canonical_snapshot_bytes(stale)))
    except live.CrawlRejected:
        session.rollback()
    finally:
        session.close()

    session = session_factory()
    loaded = load_latest_snapshot_bytes(session)
    session.close()
    assert loaded is not None
    assert json.loads(loaded[0].decode())["fetchedAt"] == first["fetchedAt"]


def test_read_only_role_cannot_write(isolated_engine: Engine) -> None:
    if not os.getenv("EV_CHARGING_SNAPSHOT_TEST_READER_URL", "").strip():
        pytest.skip("EV_CHARGING_SNAPSHOT_TEST_READER_URL not set")
    reader_engine = create_engine(os.getenv("EV_CHARGING_SNAPSHOT_TEST_READER_URL", "").strip())
    with reader_engine.connect() as conn:
        with pytest.raises(SQLAlchemyError):
            conn.execute(text("INSERT INTO ev_charging_latest_snapshot (source) VALUES ('emapna')"))
        conn.rollback()
        with pytest.raises(SQLAlchemyError):
            conn.execute(text("DELETE FROM ev_charging_latest_snapshot"))
        conn.rollback()
    reader_engine.dispose()


def test_publish_order_db_before_notify_monkeypatched(tmp_path, monkeypatch, stub_ev_charging_db):
    calls: list[str] = []

    def fake_publish(snapshot: dict) -> tuple[str, bytes]:
        calls.append("publish")
        raw = canonical_snapshot_bytes(snapshot)
        return sha256_hex(raw), raw

    def fake_notify(snapshot: dict, digest: str) -> None:
        calls.append("notify")

    monkeypatch.setenv("EV_CHARGING_SNAPSHOT_PATH", str(tmp_path / "latest.json"))
    monkeypatch.setattr("app.tasks.ev_charging.publish_snapshot", fake_publish)
    monkeypatch.setattr("app.tasks.ev_charging.notify_seenad", fake_notify)
    monkeypatch.setattr("app.tasks.ev_charging.extract_snapshot", lambda: _snapshot())

    monkeypatch.setenv("EV_CHARGING_HOURLY_ENABLED", "1")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    from unittest.mock import MagicMock

    mock_redis = MagicMock(set=MagicMock(return_value=True), eval=MagicMock())
    monkeypatch.setattr("app.tasks.ev_charging.Redis.from_url", lambda *a, **k: mock_redis)

    from app.tasks import ev_charging as tasks

    result = tasks.ev_charging_hourly_crawl()
    assert result["status"] == "notified"
    assert calls == ["publish", "notify"]