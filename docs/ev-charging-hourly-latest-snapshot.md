# eMapna hourly crawler — SeenAd scraper

Status: **feature branch, disabled by default. No production source connection or deployment has been claimed.**

## Purpose

The scraper server (not SeenAd web backend) retrieves the full public eMapna station directory, device list, and detailed connectors **once per hour**. Only the most recent **fully valid snapshot** is stored. No historical rows or versioned JSON files.

### Schedule and queue

- `EV_CHARGING_HOURLY_ENABLED=0` by default.
- Celery Beat task `tasks.ev_charging_hourly_crawl` at `UTC minute=35` of every hour (Tehran `:05`).
- Isolated `ev_charging_tasks` worker, concurrency 1, Redis NX lock with 3300-second TTL.
- Start the optional worker after QA with `docker compose --profile charging up -d worker_charging`.
- Other scraper tasks and queues are unchanged.

### Sources

All three existing public APIs on `https://gen.emapna.com`:

1. `POST /api/charger/filter/search` body `{"stationName":""}`.
2. `POST /api/charger/filter/v2` body `{"filters":[]}`.
3. `POST /api/charger/filter/v2/getStation` body `{"id":"<charger-id>"}` for **every** device.

Connector `statusCode` in detailed `getStation` is authoritative. 1 available, 2 charging, 3 unavailable, other codes unknown. List-level `connectorsCount.available` is not trusted.

Previous Staging host IP was denied by eMapna HTTP 403. **Run a controlled access check from the actual scraper host** before enabling this job. Never bypass WAF, login or rate limits.

### Completeness and atomic publication

- Validate nonempty station/device/connector arrays, unique stable IDs, exact station/device mapping, numeric status codes and calculated counts.
- Stop without publication on 401, 403, 429, missing detail, unmatched name, schema drift or incomplete results.
- Configured floors default to stations>=100, devices>=150, connectors>=200 (historical comparison 155/252/416, not hardcoded exact totals).
- If a prior complete snapshot exists, reject a >10% count decrease.
- Single file `data/ev_charging/latest.json`, replaced with `os.replace` after complete validation; `data/` is gitignored.
- The file persists across container restarts via `./:/app` mounts on backend + charging worker. Keep filesystem permissions restrictive.

### Webhook and snapshot contract

Only after atomic publication, POST signed event to SeenAd:

```json
{
  "event": "ev_charging.snapshot.completed",
  "sha256": "<64 lowercase hex of exact latest.json bytes>",
  "fetchedAt": "<UTC ISO8601 timestamp>",
  "counts": {"stations": 155, "devices": 252, "connectors": 416}
}
```

These counts are **illustrative historical values**, not live measurements or acceptance thresholds.

The request has `X-Seenad-Timestamp` and `X-Seenad-Signature`. Signature is HMAC-SHA256 over `ASCII(timestamp) + b"." + raw-body-bytes`, using shared `EV_CHARGING_WEBHOOK_SECRET`.

SeenAd callback does **not** accept a data payload or source URL. Its worker pulls the snapshot from the scraper's protected `GET /internal/ev-charging/snapshot`, Bearer-token authenticated, verifying the exact SHA256 before replacing the single current DB row. Never expose this route without TLS/IP firewall or via an untrusted redirect.

If webhook delivery fails after publication, latest.json remains valid and a bounded delayed Celery task retries webhook notification **without a fresh crawl**. A retry whose expected SHA is no longer current exits as `superseded`. The next successful hourly crawl also notifies a new snapshot. Alert on missing callbacks. No partial reversion.

### Environment (secrets must be placed only in server .env)

```ini
EV_CHARGING_HOURLY_ENABLED=0
EV_CHARGING_SEENAD_WEBHOOK_URL=https://<seenad-host>/api/v1/internal/ev-charging/webhook
EV_CHARGING_WEBHOOK_SECRET=<separate-32-character-plus-high-entropy-secret>
EV_CHARGING_SNAPSHOT_READ_TOKEN=<32-character-plus-high-entropy-token>
EV_CHARGING_SNAPSHOT_PATH=data/ev_charging/latest.json
EV_CHARGING_DETAIL_DELAY_SECONDS=0.15
EV_CHARGING_MIN_STATIONS=100
EV_CHARGING_MIN_DEVICES=150
EV_CHARGING_MIN_CONNECTORS=200
```

The same two secret values are configured separately on SeenAd. Do not use application login/consumer tokens. Apply a reverse proxy/private address for snapshot read endpoint.

### Tests and rollout

- Offline isolated fixture tests: `pytest tests/test_ev_charging_snapshot.py -q`.
- Independent server QA: confirm Beat disabled; verify parser with provider-approved network; inspect 403/429 failures and no-overwrite behavior; validate current counts, image paths and device-name matches.
- Deploy SeenAd + migrate 358 first (separate authorization), then scraper, then one manual crawl, then opt-in hourly schedule.
- Rollback: disable flag, stop only `worker_charging`, keep last complete snapshot; there is no history to purge.
