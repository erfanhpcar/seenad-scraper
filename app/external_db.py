"""
Process-local SQL Server (scraper / Divar) connectivity via SQLAlchemy + pymssql.

Celery prefork: the engine is created lazily per OS process (pid check) so child
workers never inherit a parent connection pool. Call ``reset_external_db_engine()``
from ``worker_process_init`` to discard any stale pool after fork.
"""

from __future__ import annotations

import logging
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar

import pymssql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.engine.base import Connection

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# FreeTDS / DB-Lib transient signatures (e.g. 20047 + nested 20003 "connection timed out").
_TRANSIENT_MESSAGE_FRAGMENTS = (
    "adaptive server connection timed out",
    "dbprocess is dead",
    "connection timed out",
    "connection is closed",
    "connection reset",
    "connection lost",
    "broken pipe",
    "unable to connect",
    "server is unavailable",
    "communication link failure",
)
_TRANSIENT_DBERROR_CODES = frozenset({20003, 20047, 20009, 20046})


@dataclass(frozen=True)
class ExternalDbSettings:
    host: str
    user: str
    password: str
    database: str
    port: int
    login_timeout: int
    query_timeout: int
    pool_size: int
    max_overflow: int
    pool_recycle: int
    retry_attempts: int
    retry_base_delay: float
    retry_max_delay: float

    @classmethod
    def from_env(cls) -> ExternalDbSettings:
        def _int(name: str, default: int, *, minimum: int = 1) -> int:
            raw = os.getenv(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError:
                value = default
            return max(value, minimum)

        def _float(name: str, default: float, *, minimum: float = 0.0) -> float:
            raw = os.getenv(name, str(default)).strip()
            try:
                value = float(raw)
            except ValueError:
                value = default
            return max(value, minimum)

        port_raw = os.getenv("EXT_DB_PORT", "1433").strip()
        try:
            port = int(port_raw)
        except ValueError:
            port = 1433

        return cls(
            host=os.getenv("EXT_DB_HOST", "").strip(),
            user=os.getenv("EXT_DB_USER", "").strip(),
            password=os.getenv("EXT_DB_PASS", ""),
            database=os.getenv("EXT_DB_NAME", "").strip(),
            port=port,
            login_timeout=_int("EXT_DB_LOGIN_TIMEOUT", 30),
            query_timeout=_int("EXT_DB_QUERY_TIMEOUT", 120),
            pool_size=_int("EXT_DB_POOL_SIZE", 2),
            max_overflow=_int("EXT_DB_POOL_MAX_OVERFLOW", 2, minimum=0),
            pool_recycle=_int("EXT_DB_POOL_RECYCLE", 1800, minimum=60),
            retry_attempts=_int("EXT_DB_RETRY_ATTEMPTS", 4),
            retry_base_delay=_float("EXT_DB_RETRY_BASE_DELAY", 2.0, minimum=0.1),
            retry_max_delay=_float("EXT_DB_RETRY_MAX_DELAY", 60.0, minimum=1.0),
        )

    def validate(self) -> None:
        if not self.host or not self.user or not self.password or not self.database:
            raise RuntimeError(
                "Missing external DB config. Set EXT_DB_HOST, EXT_DB_USER, EXT_DB_PASS, EXT_DB_NAME."
            )


_engine: Engine | None = None
_engine_pid: int | None = None


def _build_engine_url(settings: ExternalDbSettings) -> URL:
    return URL.create(
        drivername="mssql+pymssql",
        username=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )


def get_external_db_settings() -> ExternalDbSettings:
    return ExternalDbSettings.from_env()


def reset_external_db_engine() -> None:
    """Dispose the process-local engine (call after Celery fork)."""
    global _engine, _engine_pid
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_pid = None


def get_external_db_engine() -> Engine:
    """
    Return a pooled SQLAlchemy engine for the scraper MSSQL database.

    One engine (and pool) per worker child process; safe under Celery prefork.
    """
    global _engine, _engine_pid
    pid = os.getpid()
    if _engine is not None and _engine_pid == pid:
        return _engine

    if _engine is not None:
        _engine.dispose()

    settings = get_external_db_settings()
    settings.validate()

    _engine = create_engine(
        _build_engine_url(settings),
        pool_pre_ping=True,
        pool_recycle=settings.pool_recycle,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        connect_args={
            "login_timeout": settings.login_timeout,
            "timeout": settings.query_timeout,
        },
    )
    _engine_pid = pid
    LOGGER.debug(
        "external_db engine ready pid=%s pool_size=%s max_overflow=%s query_timeout=%ss",
        pid,
        settings.pool_size,
        settings.max_overflow,
        settings.query_timeout,
    )
    return _engine


@contextmanager
def external_db_connection() -> Iterator[Connection]:
    """Borrow a connection from the process-local pool; returned to pool on exit."""
    engine = get_external_db_engine()
    with engine.connect() as conn:
        yield conn


def _error_message(exc: BaseException) -> str:
    return str(exc).lower()


def _dberror_codes(exc: BaseException) -> set[int]:
    codes: set[int] = set()
    if isinstance(exc, pymssql.Error):
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            codes.add(args[0])
    orig = getattr(exc, "orig", None)
    if isinstance(orig, pymssql.Error) and getattr(orig, "args", None):
        if isinstance(orig.args[0], int):
            codes.add(orig.args[0])
    return codes


def is_transient_mssql_error(exc: BaseException) -> bool:
    """True for timeouts and dead DBPROCESS / link failures worth retrying."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if not isinstance(exc, pymssql.Error):
        sqlalchemy_err = getattr(exc, "orig", None)
        if sqlalchemy_err is not None:
            return is_transient_mssql_error(sqlalchemy_err)
        return False

    codes = _dberror_codes(exc)
    if codes & _TRANSIENT_DBERROR_CODES:
        return True

    message = _error_message(exc)
    return any(fragment in message for fragment in _TRANSIENT_MESSAGE_FRAGMENTS)


def retry_mssql_operation(
    operation: Callable[[], _T],
    *,
    settings: ExternalDbSettings | None = None,
    operation_name: str = "mssql",
) -> _T:
    """Run *operation* with exponential backoff on transient pymssql failures."""
    cfg = settings or get_external_db_settings()
    attempts = max(cfg.retry_attempts, 1)
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            if not is_transient_mssql_error(exc) or attempt >= attempts:
                raise
            reset_external_db_engine()
            delay = min(cfg.retry_max_delay, cfg.retry_base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.15)
            LOGGER.warning(
                "%s transient failure attempt=%s/%s sleep=%.1fs error=%s",
                operation_name,
                attempt,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result.mappings()]


def fetch_table_rows(
    table_name: str,
    *,
    limit: int | None = None,
    since: Any = None,
) -> list[dict[str, Any]]:
    """
    Fetch rows from a scraper table using the pooled engine and transient retries.
    """
    safe_table = f"[{table_name.replace(']', ']]')}]"
    settings = get_external_db_settings()
    settings.validate()

    def _run() -> list[dict[str, Any]]:
        with external_db_connection() as conn:
            if since is not None and limit is not None:
                sql = text(
                    f"SELECT TOP {int(limit)} * FROM {safe_table} "
                    "WHERE [time] > :since ORDER BY [time] ASC"
                )
                result = conn.execute(sql, {"since": since})
            elif since is not None:
                sql = text(f"SELECT * FROM {safe_table} WHERE [time] > :since ORDER BY [time] ASC")
                result = conn.execute(sql, {"since": since})
            elif limit is not None:
                sql = text(f"SELECT TOP {int(limit)} * FROM {safe_table} ORDER BY [time] DESC")
                result = conn.execute(sql)
            else:
                sql = text(f"SELECT * FROM {safe_table}")
                result = conn.execute(sql)
            return _rows_from_result(result)

    return retry_mssql_operation(
        _run,
        settings=settings,
        operation_name=f"fetch_table_rows({table_name})",
    )


def ping_external_db() -> dict[str, object]:
    """Open pool connection, run SELECT 1, return status dict (no secrets)."""
    settings = get_external_db_settings()
    if not settings.host or not settings.user or not settings.password or not settings.database:
        return {
            "ok": False,
            "error": "Missing EXT_DB_HOST, EXT_DB_USER, EXT_DB_PASS, or EXT_DB_NAME",
            "error_type": "RuntimeError",
            "host": settings.host or None,
            "database": settings.database or None,
            "port": settings.port,
        }
    try:
        def _run() -> None:
            with external_db_connection() as conn:
                conn.execute(text("SELECT 1")).scalar_one()

        retry_mssql_operation(_run, settings=settings, operation_name="ping_external_db")
        return {
            "ok": True,
            "host": settings.host,
            "database": settings.database,
            "port": settings.port,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "host": settings.host or None,
            "database": settings.database or None,
            "port": settings.port,
        }
