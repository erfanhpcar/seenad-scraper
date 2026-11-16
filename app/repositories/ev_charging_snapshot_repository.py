"""Persistence for the latest-only EV charging snapshot row (source=emapna)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EvChargingLatestSnapshot
from src.scraper.ev_charging_live import CrawlRejected

LOGGER = logging.getLogger(__name__)

SOURCE_EMAPNA = "emapna"
_MAX_BYTES = 8_000_000


def canonical_snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise CrawlRejected("snapshot exceeds 8MB limit")
    return raw


def sha256_hex(raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    if len(digest) != 64:
        raise CrawlRejected("invalid sha256 digest")
    return digest


def _parse_fetched_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CrawlRejected("invalid fetchedAt") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _apply_transition_guards(
    previous: EvChargingLatestSnapshot | None,
    snapshot: dict[str, Any],
    raw: bytes,
    digest: str,
) -> None:
    fetched_at = _parse_fetched_at(str(snapshot["fetchedAt"]))
    if previous is None:
        return

    if previous.sha256 == digest:
        if previous.fetched_at == fetched_at:
            raise CrawlRejected("duplicate snapshot sha256")
        raise CrawlRejected("duplicate snapshot sha256")

    prev_time = previous.fetched_at.astimezone(UTC)
    if fetched_at < prev_time:
        raise CrawlRejected("out-of-order snapshot; previous is newer")
    if fetched_at == prev_time:
        raise CrawlRejected("same timestamp with different snapshot content")

    try:
        prev_payload = previous.payload if isinstance(previous.payload, dict) else json.loads(
            previous.snapshot_bytes.decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise CrawlRejected("stored previous snapshot is unreadable") from exc
    prev_counts = prev_payload.get("counts")
    if isinstance(prev_counts, dict):
        for kind in ("stations", "devices", "connectors"):
            new_val = snapshot["counts"][kind]
            old_val = prev_counts.get(kind)
            if isinstance(old_val, int) and new_val < old_val * 0.9:
                raise CrawlRejected(f"{kind} declined >10%; retain previous snapshot")


def upsert_latest_snapshot(session: Session, snapshot: dict[str, Any], raw: bytes, digest: str) -> None:
    """Replace the singleton row inside the caller's open transaction."""
    fetched_at = _parse_fetched_at(str(snapshot["fetchedAt"]))
    now = datetime.now(UTC)

    existing = session.execute(
        select(EvChargingLatestSnapshot)
        .where(EvChargingLatestSnapshot.source == SOURCE_EMAPNA)
        .with_for_update()
    ).scalar_one_or_none()

    _apply_transition_guards(existing, snapshot, raw, digest)

    if existing is None:
        row = EvChargingLatestSnapshot(
            source=SOURCE_EMAPNA,
            sha256=digest,
            fetched_at=fetched_at,
            updated_at=now,
            payload=snapshot,
            snapshot_bytes=raw,
        )
        session.add(row)
        return

    existing.sha256 = digest
    existing.fetched_at = fetched_at
    existing.updated_at = now
    existing.payload = snapshot
    existing.snapshot_bytes = raw


def load_latest_snapshot_bytes(session: Session) -> tuple[bytes, str] | None:
    row = session.execute(
        select(EvChargingLatestSnapshot).where(EvChargingLatestSnapshot.source == SOURCE_EMAPNA)
    ).scalar_one_or_none()
    if row is None:
        return None
    stored_digest = hashlib.sha256(row.snapshot_bytes).hexdigest()
    if stored_digest != row.sha256:
        raise CrawlRejected("stored snapshot_bytes digest mismatch")
    return row.snapshot_bytes, row.sha256


def load_latest_snapshot_row(session: Session) -> EvChargingLatestSnapshot | None:
    return session.execute(
        select(EvChargingLatestSnapshot).where(EvChargingLatestSnapshot.source == SOURCE_EMAPNA)
    ).scalar_one_or_none()