"""Private authenticated read endpoint for the single latest complete snapshot."""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

from src.scraper.ev_charging_live import load_published_snapshot_bytes

router = APIRouter()


@router.get("/internal/ev-charging/snapshot")
def get_latest_ev_charging_snapshot(authorization: str | None = Header(default=None)) -> Response:
    token = os.getenv("EV_CHARGING_SNAPSHOT_READ_TOKEN", "").strip()
    bearer = authorization or ""
    incoming = bearer[len("Bearer "):].strip() if bearer.startswith("Bearer ") else ""
    if len(token) < 32 or not hmac.compare_digest(token, incoming):
        raise HTTPException(status_code=401, detail="Unauthorized")
    loaded = load_published_snapshot_bytes()
    if loaded is None:
        raise HTTPException(status_code=404, detail="No published snapshot")
    raw, digest = loaded
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=503, detail="Snapshot too large")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid snapshot") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1 or payload.get("source") != "emapna":
        raise HTTPException(status_code=503, detail="Invalid snapshot version")
    stored_digest = hashlib.sha256(raw).hexdigest()
    if stored_digest != digest:
        raise HTTPException(status_code=503, detail="Snapshot integrity mismatch")
    return Response(
        content=raw,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "X-Content-SHA256": digest,
        },
    )
