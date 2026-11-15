"""Strict hourly eMapna extraction and single-file latest-snapshot publication.

All detail requests must succeed before the latest snapshot is replaced.
The only durable artifact is data/ev_charging/latest.json (no history).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://gen.emapna.com/api/charger/filter"
_STATUS = {1: "Available", 2: "Charging", 3: "Unavailable"}
_MAX_BYTES = 8_000_000


class CrawlRejected(RuntimeError):
    """Source rejected, produced incomplete data, or its schema changed."""


def _normalized_name(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def _session() -> requests.Session:
    session = requests.Session()
    # Never retry source denials (401, 403, 429).
    retries = Retry(
        total=2, connect=2, read=1, backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "SeenAd-Charging-Snapshot/1.0"})
    return session


def _post(session: requests.Session, suffix: str, body: dict) -> object:
    url = f"{BASE_URL}/{suffix}"
    response = session.post(url, json=body, timeout=(6, 18), allow_redirects=False)
    if response.status_code in (401, 403, 429):
        raise CrawlRejected(f"eMapna source blocked request: {suffix} HTTP {response.status_code}")
    if not 200 <= response.status_code < 300:
        raise CrawlRejected(f"eMapna {suffix} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CrawlRejected(f"eMapna {suffix} returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise CrawlRejected(f"eMapna {suffix} contract changed")
    if payload.get("result", {}).get("data") is None:
        raise CrawlRejected(f"eMapna {suffix} has no result.data")
    return payload["result"]["data"]


def _list(value: object, label: str) -> list:
    if not isinstance(value, list) or not value:
        raise CrawlRejected(f"{label}: expected nonempty list")
    return value


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def extract_snapshot(session: requests.Session | None = None) -> dict:
    """Fetch complete directory and every device detail; fail closed on gaps."""
    owned = session is None
    session = session or _session()
    try:
        directory = _list(_post(session, "search", {"stationName": ""}), "stations")
        response = _post(session, "v2", {"filters": []})
        if not isinstance(response, dict):
            raise CrawlRejected("charger list format changed")
        chargers = _list(response.get("chargers"), "chargers")

        stations: dict[str, dict] = {}
        names: dict[str, list[str]] = {}
        for row in directory:
            if not isinstance(row, dict):
                raise CrawlRejected("non-object station row")
            sid = str(row.get("stationId") or "").strip()
            name = str(row.get("stationName") or "").strip()
            if not sid or not name or sid in stations:
                raise CrawlRejected("missing/duplicate station ID or name")
            coords = row.get("stationCoordinate") or {}
            if not isinstance(coords, dict):
                coords = {}
            stations[sid] = {
                "stationId": sid,
                "name": name,
                "address": row.get("address"),
                "latitude": coords.get("lat"),
                "longitude": coords.get("lon"),
                "aggregateStatus": row.get("stationStatus"),
                "statusDescription": row.get("stationStatusDes"),
                "devices": [],
            }
            names.setdefault(_normalized_name(name), []).append(sid)

        visited: set[str] = set()
        for i, charger in enumerate(chargers):
            if not isinstance(charger, dict):
                raise CrawlRejected("non-object charger row")
            device_id = str(charger.get("id") or "").strip()
            if not device_id or device_id in visited:
                raise CrawlRejected("missing/duplicate charger ID")
            visited.add(device_id)
            detail = _post(session, "v2/getStation", {"id": device_id})
            info = detail.get("stationInfo") if isinstance(detail, dict) else None
            if not isinstance(info, dict):
                raise CrawlRejected(f"missing getStation.stationInfo for {device_id}")
            connector_rows = info.get("connectors")
            if not isinstance(connector_rows, list) or not connector_rows:
                raise CrawlRejected(f"missing detailed connectors for {device_id}")
            source_sid = str(info.get("stationId") or charger.get("stationId") or "").strip()
            if source_sid and source_sid not in stations:
                raise CrawlRejected(f"unknown station ID on {device_id}")
            if not source_sid:
                key = _normalized_name(charger.get("stationName") or info.get("name"))
                matches = names.get(key, [])
                if len(matches) != 1:
                    raise CrawlRejected(f"unmatched/ambiguous station name for {device_id}: {key}")
                source_sid = matches[0]
            connectors = []
            connector_ids: set[str] = set()
            for index, record in enumerate(connector_rows):
                if not isinstance(record, dict):
                    raise CrawlRejected(f"invalid connector for {device_id}")
                cid = str(record.get("id") or "").strip()
                if not cid or cid in connector_ids:
                    raise CrawlRejected(f"missing/duplicate connector ID on {device_id}")
                connector_ids.add(cid)
                code = _int_or_none(record.get("statusCode"))
                name = record.get("connectorName")
                connectors.append({
                    "connectorId": cid,
                    "compositeKey": f"{device_id}::{cid}",
                    "connectorName": name,
                    "standard": str(name).split("-")[-1] if name else None,
                    "acDc": record.get("connectorType"),
                    "powerKw": record.get("power"),
                    "statusCode": code,
                    "status": _STATUS.get(code),
                    "statusLabelFa": record.get("status"),
                    "socPercent": record.get("percentage"),
                    "statusUpdatedAt": (info.get("plan") or {}).get("time")
                    if isinstance(info.get("plan"), dict) else None,
                })
            image_records = info.get("images") or []
            images = [
                image.get("data_url") for image in image_records
                if isinstance(image, dict) and isinstance(image.get("data_url"), str)
                and image["data_url"].startswith("https://")
            ] if isinstance(image_records, list) else []
            stations[source_sid]["devices"].append({
                "deviceId": device_id,
                "stationId": source_sid,
                "name": info.get("name") or charger.get("stationName"),
                "deviceStatus": info.get("stationStatus") or charger.get("chargerStatus"),
                "listStatus": charger.get("chargerStatus"),
                "statusUpdatedAt": (info.get("plan") or {}).get("time")
                if isinstance(info.get("plan"), dict) else None,
                "powerKw": info.get("power"),
                "tariff": info.get("tariff"),
                "currencyUnit": info.get("currencyUnit"),
                "isFree": info.get("isFree"),
                "reservableRaw": (
                    info["reservable"] if info.get("reservable") is not None
                    else charger.get("reservable")
                ),
                "workDays": info.get("workDays"),
                "workHours": info.get("workHours"),
                "facilities": info.get("facilities"),
                "imageUrls": images,
                "connectors": connectors,
            })
            if i < len(chargers) - 1:
                time.sleep(max(0.05, float(os.getenv("EV_CHARGING_DETAIL_DELAY_SECONDS", "0.15"))))

        sorted_stations = [stations[key] for key in sorted(stations)]
        for row in sorted_stations:
            row["devices"].sort(key=lambda d: d["deviceId"])
        counts = {
            "stations": len(sorted_stations),
            "devices": len(chargers),
            "connectors": sum(
                len(d["connectors"]) for s in sorted_stations for d in s["devices"]
            ),
        }
        # Strict floors are configured below known historical 155/252/416.
        for kind, floor in (
            ("stations", int(os.getenv("EV_CHARGING_MIN_STATIONS", "100"))),
            ("devices", int(os.getenv("EV_CHARGING_MIN_DEVICES", "150"))),
            ("connectors", int(os.getenv("EV_CHARGING_MIN_CONNECTORS", "200"))),
        ):
            if counts[kind] < floor:
                raise CrawlRejected(f"incomplete {kind}: {counts[kind]} < {floor}")
        result = {
            "schemaVersion": 1,
            "source": "emapna",
            "fetchedAt": datetime.now(UTC).isoformat(),
            "counts": counts,
            "stations": sorted_stations,
        }
        validate_snapshot(result)
        return result
    finally:
        if owned:
            session.close()


def validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("schemaVersion") != 1 or snapshot.get("source") != "emapna":
        raise CrawlRejected("unsupported charging snapshot version/source")
    stations = _list(snapshot.get("stations"), "snapshot.stations")
    ids: set[str] = set()
    devices: set[str] = set()
    count_connectors = 0
    for station in stations:
        if not isinstance(station, dict) or not station.get("stationId"):
            raise CrawlRejected("invalid station in snapshot")
        sid = str(station["stationId"])
        if sid in ids:
            raise CrawlRejected("duplicate station ID")
        ids.add(sid)
        for device in station.get("devices", []):
            did = str(device.get("deviceId") or "")
            if not did or did in devices or device.get("stationId") != sid:
                raise CrawlRejected("invalid/duplicate device or relation")
            devices.add(did)
            connectors = device.get("connectors")
            if not isinstance(connectors, list) or not connectors:
                raise CrawlRejected("device missing connectors")
            seen: set[str] = set()
            for connector in connectors:
                key = str(connector.get("compositeKey") or "")
                if not key or key in seen or not key.startswith(did + "::"):
                    raise CrawlRejected("invalid/duplicate connector identity")
                seen.add(key)
                count_connectors += 1
    if snapshot.get("counts") != {
        "stations": len(ids), "devices": len(devices), "connectors": count_connectors,
    }:
        raise CrawlRejected("snapshot count mismatch")


def snapshot_path() -> Path:
    return Path(os.getenv("EV_CHARGING_SNAPSHOT_PATH", "data/ev_charging/latest.json"))


def publish_snapshot(snapshot: dict) -> tuple[str, bytes]:
    """Atomic replacement: only latest valid snapshot, never a version archive."""
    validate_snapshot(snapshot)
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise CrawlRejected("snapshot exceeds 8MB limit")
    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Compare floors with the previous complete snapshot; never truncate on
    # mass source loss even if static configured minimum is passed.
    if target.is_file():
        previous = json.loads(target.read_text(encoding="utf-8"))
        for kind in ("stations", "devices", "connectors"):
            if snapshot["counts"][kind] < previous["counts"][kind] * 0.9:
                raise CrawlRejected(f"{kind} declined >10%; retain previous snapshot")
        old_time = datetime.fromisoformat(previous["fetchedAt"])
        if datetime.fromisoformat(snapshot["fetchedAt"]) <= old_time:
            raise CrawlRejected("out-of-order snapshot; previous is newer")
    staging = target.with_name(target.name + ".pending")
    try:
        staging.write_bytes(raw)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    LOGGER.info("ev_charging.snapshot_published digest=%s counts=%s", digest, snapshot["counts"])
    return digest, raw


def notify_seenad(snapshot: dict, digest: str) -> None:
    """Signed pointer webhook only; SeenAd independently pulls exact snapshot."""
    target = os.getenv("EV_CHARGING_SEENAD_WEBHOOK_URL", "").strip()
    secret = os.getenv("EV_CHARGING_WEBHOOK_SECRET", "").strip()
    if not target.startswith("https://") or len(secret) < 32:
        raise CrawlRejected("HTTPS webhook URL and 32+-character secret are required")
    event = {
        "event": "ev_charging.snapshot.completed",
        "sha256": digest,
        "fetchedAt": snapshot["fetchedAt"],
        "counts": snapshot["counts"],
    }
    raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stamp = str(int(time.time()))
    sig = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    response = requests.post(target, data=raw, headers={
        "Content-Type": "application/json",
        "X-Seenad-Timestamp": stamp,
        "X-Seenad-Signature": sig,
    }, timeout=(5, 12), allow_redirects=False)
    if response.status_code != 202:
        raise CrawlRejected(f"SeenAd webhook rejected snapshot: HTTP {response.status_code}")
