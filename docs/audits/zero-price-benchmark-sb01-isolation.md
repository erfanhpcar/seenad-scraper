# SB-01 — Legacy Benchmark Writer Isolation

**Repository:** `erfanhpcar/seenad-scraper`  
**Change:** Isolate deprecated scraper-side benchmark DB writer while `seenad-main` performs remediation.

---

## What changed

| Control | Default (production compose) | Purpose |
|---------|------------------------------|---------|
| `ZERO_PRICE_BENCHMARK_BEAT_ENABLED` | `0` on `seenad_beat` | Stop enqueueing `tasks.run_daily_zero_price_benchmark` |
| `BENCHMARK_ENABLED` | `0` on `benchmark_worker` profile | Worker profile stays off; no accidental enable via compose |
| `BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED` | unset / `0` | **Required** (with `BENCHMARK_ENABLED=1`) for any benchmark DB write |

Runtime guards call `assert_legacy_benchmark_writer_may_run()` in:

- `app/tasks/zero_price_benchmark.py` (Celery task → `Ignore`)
- `src/zero_price_benchmark/runner.py`
- `src/zero_price_benchmark/automap.py`
- `scripts/run_benchmark_pipeline.sh`, `scripts/auto_map_zero_price_benchmark.py`, `scripts/ensure_benchmark_worker.sh`

Implementation code remains in the tree for a future, explicitly authorized re-enable.

---

## Pending Redis messages (`scheduled_tasks`)

**Observed on scraper node (2026-10-20 audit, read-only):**

| Metric | Value |
|--------|------:|
| Queue name | `scheduled_tasks` |
| `LLEN scheduled_tasks` | **5** |
| Task name in sample payload | `tasks.run_daily_zero_price_benchmark` |
| Cause | Beat had `ZERO_PRICE_BENCHMARK_BEAT_ENABLED=1` while no consumer ran the queue |

**This change does not purge Redis.** Messages remain until handled in a controlled cutover.

---

## Safe procedure: inspect legacy benchmark messages only

Use only during an **authorized** cutover window. Do not run `FLUSHDB` / broad queue purge.

### 1. Inspect (read-only)

```bash
# Queue depth
docker compose -p project exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LLEN scheduled_tasks'

# Peek one message (headers only — no body secrets)
docker compose -p project exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LRANGE scheduled_tasks 0 0'
```

Confirm `headers.task` is `tasks.run_daily_zero_price_benchmark` before removing anything.

### 2. Optional: drain without executing writes

With SB-01 deployed and **without** `BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED=1`:

- Starting `benchmark_worker` is blocked by `ensure_benchmark_worker.sh`.
- If a worker consumed messages anyway, the task would `Ignore` and perform **no DB writes**.

For a clean queue without running legacy logic, prefer **targeted removal** after inspection (operations approval required):

```bash
# Example: remove ONE message from the head after verifying task name (authorized ops only)
docker compose -p project exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli LPOP scheduled_tasks'
```

Repeat until `LLEN` is 0, or stop after non-benchmark tasks appear (investigate before popping).

### 3. Re-enable scraper writer (future, not SB-01)

Only after main-app schema compatibility is verified and documented:

1. Set `BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED=1` in deployment secrets (not committed).
2. Configure `secrets/benchmark_database_url` (main DB).
3. Set `ZERO_PRICE_BENCHMARK_BEAT_ENABLED=1` only if scraper should schedule again.
4. Set `BENCHMARK_ENABLED=1` on `benchmark_worker` and start `--profile benchmark`.

---

## Rollout precautions

1. **Deploy code + compose env** — restart `seenad_beat` in a maintenance window so Beat reloads schedule (no benchmark entries when `ZERO_PRICE_BENCHMARK_BEAT_ENABLED=0`).
2. **Do not start `benchmark_worker`** — profile `benchmark` remains optional and guarded.
3. **Leave Redis queue intact** until ops follows the inspection procedure above.
4. **Verify unrelated jobs** — Bama/IranJib ingestion, USD rate, sell-news, listing feeder beats unchanged in compose.
5. **seenad-main** continues as sole benchmark writer; scraper stops adding new scheduled benchmark tasks after beat restart.

---

## Verification

```bash
docker compose -p project exec -T backend pytest tests/test_zero_price_benchmark_sb01_isolation.py -q
```

Expected: benchmark beat key absent with production-like env; unrelated beat keys present; guard requires `BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED=1`.
