#!/usr/bin/env python3
"""Create or rotate seenad_ev_snapshot_reader (idempotent). Never prints the password."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import engine  # noqa: E402

HOST_HANDOFF_PATH = Path("/root/.seenad-ev-db-reader.env")
CONTAINER_HANDOFF_PATH = Path("/root/.seenad-ev-db-reader.env")


def _handoff_path() -> Path:
    if HOST_HANDOFF_PATH.parent.is_dir() and os.access(HOST_HANDOFF_PATH.parent, os.W_OK):
        return HOST_HANDOFF_PATH
    return CONTAINER_HANDOFF_PATH
ROLE_NAME = "seenad_ev_snapshot_reader"


def _load_or_create_password(force_rotate: bool) -> str:
    path = _handoff_path()
    if path.is_file() and not force_rotate:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("EV_CHARGING_DB_READER_PASSWORD="):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 32:
                    return value
    return secrets.token_urlsafe(48)


def _write_handoff(password: str) -> None:
    lines = [
        "# SeenAd EV charging DB reader — scraper PostgreSQL (transfer to main server securely)",
        "EV_CHARGING_DB_READER_HOST=188.121.120.138",
        "EV_CHARGING_DB_READER_PORT=5432",
        "EV_CHARGING_DB_READER_DATABASE=seenad_db",
        "EV_CHARGING_DB_READER_USER=seenad_ev_snapshot_reader",
        "EV_CHARGING_DB_READER_SSLMODE=verify-full",
        "EV_CHARGING_DB_READER_SSLROOTCERT=/etc/seenad/ev-charging-db-ca.crt",
        f"EV_CHARGING_DB_READER_PASSWORD={password}",
    ]
    path = _handoff_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    if path != HOST_HANDOFF_PATH and HOST_HANDOFF_PATH.parent.is_dir():
        try:
            HOST_HANDOFF_PATH.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(HOST_HANDOFF_PATH, 0o600)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rotate", action="store_true", help="Generate a new password and update role")
    args = parser.parse_args()

    password = _load_or_create_password(args.rotate)
    escaped = password.replace("'", "''")

    statements = [
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE_NAME}') THEN
                CREATE ROLE {ROLE_NAME}
                    LOGIN
                    NOSUPERUSER
                    NOCREATEDB
                    NOCREATEROLE
                    NOREPLICATION
                    NOBYPASSRLS
                    PASSWORD '{escaped}';
            ELSE
                ALTER ROLE {ROLE_NAME} WITH PASSWORD '{escaped}';
            END IF;
        END
        $$;
        """,
        f"GRANT CONNECT ON DATABASE {os.getenv('POSTGRES_DB', 'seenad_db')} TO {ROLE_NAME};",
        f"GRANT USAGE ON SCHEMA public TO {ROLE_NAME};",
        f"GRANT SELECT ON TABLE public.ev_charging_latest_snapshot TO {ROLE_NAME};",
        f"REVOKE ALL ON TABLE public.ev_charging_latest_snapshot FROM PUBLIC;",
    ]

    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))

    _write_handoff(password)
    print(f"provisioned role={ROLE_NAME} handoff={_handoff_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())