"""Private authenticated read endpoint for the single latest complete snapshot."""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

from src.scraper.ev_charging_live import snapshot_path

router = APIRouter()


@router.get("/internal/ev-charging/snapshot")
def get_latest_ev_charging_snapshot(authorization: str | None = Header(default=None)) -> Response:
    token = os.getenv("EV_CHARGING_SNAPSHOT_READ_TOKEN", "").strip()
    incoming = (authorization or "").removeprefix("Bearer ").strip()
    if len(token) < 32 or not hmac.compare_digest(token, incoming):
        raise HTTPException(status_code=401, detail="Unauthorized")
    source = snapshot_path()
    if not source.is_file():
        raise HTTPException(status_code=404, detail="No published snapshot")
    raw = source.read_bytes()
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=503, detail="Snapshot too large")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid snapshot") from exc
    if payload.get("schemaVersion") != 1 or payload.get("source") != "emapna":
        raise HTTPException(status_code=503, detail="Invalid snapshot version")
    return Response(
        content=raw,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "X-Content-SHA256": hashlib.sha256(raw).hexdigest(),
        },
    )
