"""Shared pytest fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.repositories import ev_charging_snapshot_repository as repo


@dataclass
class _FakeLatestRow:
    sha256: str
    fetched_at: datetime
    payload: dict[str, Any]
    snapshot_bytes: bytes


@pytest.fixture
def stub_ev_charging_db(monkeypatch):
    state: dict[str, _FakeLatestRow] = {}

    def _upsert(_session: object, snapshot: dict[str, Any], raw: bytes, digest: str) -> None:
        existing = None
        if "emapna" in state:
            row = state["emapna"]
            existing = type("Row", (), {
                "sha256": row.sha256,
                "fetched_at": row.fetched_at,
                "payload": row.payload,
                "snapshot_bytes": row.snapshot_bytes,
            })()
        repo._apply_transition_guards(existing, snapshot, raw, digest)
        fetched_at = datetime.fromisoformat(snapshot["fetchedAt"]).astimezone(UTC)
        state["emapna"] = _FakeLatestRow(
            sha256=digest,
            fetched_at=fetched_at,
            payload=snapshot,
            snapshot_bytes=raw,
        )

    class _StubSession:
        def begin(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(repo, "upsert_latest_snapshot", _upsert)
    monkeypatch.setattr("app.database.SessionLocal", lambda: _StubSession())
    return state