"""Isolated PostgreSQL DSN helpers for zero_price batch integration tests."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_SCHEMA_SQL: Path = REPO_ROOT / "tests" / "sql" / "zero_price_batch_integration_schema.sql"

FORBIDDEN_DATABASE_NAMES: frozenset[str] = frozenset({"seenad_db", "postgres"})

OPT_IN_ENV = "ZERO_PRICE_BATCH_PG_TESTS"
TEST_DATABASE_URL_ENV = "ZERO_PRICE_BATCH_TEST_DATABASE_URL"
ALLOWLIST_ENV = "ZERO_PRICE_BATCH_TEST_ALLOWED_DB_NAMES"


class ZeroPriceBatchTestDatabaseRejected(RuntimeError):
    """Raised when the configured database is not an approved isolated test target."""


def integration_opt_in() -> bool:
    return os.getenv(OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes"}


def _extra_allowed_database_names() -> frozenset[str]:
    raw = os.getenv(ALLOWLIST_ENV, "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def assert_isolated_test_database_url(database_url: str) -> str:
    """
    Fail-closed guard: only databases explicitly marked as test targets are allowed.

    Allowed when the database name:
    - ends with ``_test``, or
    - is listed in ``ZERO_PRICE_BATCH_TEST_ALLOWED_DB_NAMES``.

    Never allowed: known operational names (e.g. ``seenad_db``).
    """

    url = database_url.strip()
    if not url:
        raise ZeroPriceBatchTestDatabaseRejected(
            f"{TEST_DATABASE_URL_ENV} is required for PostgreSQL integration tests"
        )

    parsed = make_url(url)
    db_name = (parsed.database or "").strip()
    if not db_name:
        raise ZeroPriceBatchTestDatabaseRejected("test database URL must include a database name")

    if db_name in FORBIDDEN_DATABASE_NAMES:
        raise ZeroPriceBatchTestDatabaseRejected(
            f"refusing to run destructive integration tests against operational database '{db_name}'"
        )

    extra = _extra_allowed_database_names()
    if db_name.endswith("_test") or db_name in extra:
        return db_name

    raise ZeroPriceBatchTestDatabaseRejected(
        "refusing ambiguous database "
        f"'{db_name}': name must end with '_test' or appear in "
        f"{ALLOWLIST_ENV} (fail-closed)"
    )


def resolve_test_database_url() -> str:
    if not integration_opt_in():
        raise ZeroPriceBatchTestDatabaseRejected(
            f"{OPT_IN_ENV}=1 is required to run PostgreSQL integration tests"
        )
    url = os.getenv(TEST_DATABASE_URL_ENV, "").strip()
    assert_isolated_test_database_url(url)
    return url


def create_isolated_test_engine(database_url: str) -> Engine:
    assert_isolated_test_database_url(database_url)
    return create_engine(database_url, pool_pre_ping=True)


def create_test_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def test_database_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def apply_zero_price_batch_test_schema(engine: Engine) -> None:
    """Apply test-only snapshot + registry DDL on the isolated test database."""

    assert_isolated_test_database_url(str(engine.url))
    sql = TEST_SCHEMA_SQL.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))
