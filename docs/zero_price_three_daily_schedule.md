# Zero-price crawlers — three daily slots (Asia/Tehran)

Calculator runs on **seenad-main** at **11:00**, **13:00**, and **16:00** Asia/Tehran.
Scraper Celery Beat uses **UTC** (`enable_utc=True`).

## Beat schedule (production defaults)

| Source  | Asia/Tehran   | UTC (hour:minute) |
|---------|---------------|-------------------|
| Bama    | 10:45, 12:45, 15:45 | 07:15, 09:15, 12:15 |
| IranJib | 10:50, 12:50, 15:50 | 07:20, 09:20, 12:20 |

Environment (also set on `beat` in `docker-compose.yml`):

- `BAMA_ZERO_PRICE_BEAT_HOURS_UTC=7,9,12`
- `BAMA_ZERO_PRICE_BEAT_MINUTE_UTC=15`
- `IRANJIB_ZERO_PRICE_BEAT_HOURS_UTC=7,9,12`
- `IRANJIB_ZERO_PRICE_BEAT_MINUTE_UTC=20`

Legacy twice-daily slots (10:00 / 16:00 Tehran) are **removed** from defaults; do not set
`BAMA_ZERO_PRICE_BEAT_HOURS_UTC=6,12` in production unless intentionally rolling back.

## Batch completion — two-repo contract

**Scraper (`seenad-scraper`)** writes snapshot rows and maintains **`zero_price_scrape_batch`**
(migration `postgres/migrations/022_zero_price_scrape_batch.sql`). Table name, columns, and
status values are stable; **seenad-main** must consume only batches that are explicitly
`completed`.

Snapshot tables (`car_zero_price_scrape_snapshot`, `iranjib_zero_price_scrape_snapshot`)
commit incrementally every 400 rows during a crawl. The registry row is the only signal that
a `batch_id` is safe to read.

### Status lifecycle (scraper)

| status        | Set by scraper when | seenad-main |
|---------------|---------------------|-------------|
| `in_progress` | Crawl started (`begin_zero_price_batch`) | **Do not consume** — includes worker hard-kill (row never updated). |
| `completed`   | Crawl finished, **≥1** snapshot row persisted, counters validated | **Consume** — latest per `source` by `completed_at DESC`. |
| `failed`      | Crawl error/empty run (`abort_zero_price_batch`) | **Do not consume** — snapshot rows for that `batch_id` are deleted. |

Rules enforced in scraper pipelines:

- `completed` is never written for an empty batch (`raw_row_count > 0` and DB row count check).
- Partial commits during crawl stay `in_progress` until `complete_zero_price_batch` runs.
- Celery retry uses a **new** `batch_id`; aborted runs are `failed` with snapshots removed.

### seenad-main consumption (required)

Resolve the latest ingestible batch **only** from the registry:

```sql
SELECT batch_id, completed_at, raw_row_count
FROM zero_price_scrape_batch
WHERE source = :source   -- 'bama' | 'iranjib'
  AND status = 'completed'
ORDER BY completed_at DESC
LIMIT 1;
```

Then load snapshot rows for that `batch_id` from the matching snapshot table. Do **not** use
`MAX(created_at)` on snapshot tables alone after scraper deploy.

### Rollout order

1. Apply `022_zero_price_scrape_batch.sql` on the scraper DB.
2. Deploy scraper with registry lifecycle (this repo).
3. Deploy seenad-main with the query above for both sources.
4. Optional backfill: historical batches without registry rows remain invisible to main until
   a new `completed` crawl exists (no automatic backfill).

### PostgreSQL integration tests (opt-in, isolated database)

File: `tests/test_zero_price_batch_registry_postgres.py`

**Default:** skipped (no PostgreSQL required for normal `unittest` runs).

**Never** point these tests at the operational scraper database (`seenad_db`). A prior run
inside `seenad_backend` used `app.database` → `DATABASE_URL=...@db:5432/seenad_db` when
`ZERO_PRICE_BATCH_PG_TESTS` defaulted to enabled; that path is removed.

Opt-in steps:

1. Create an empty database whose name ends with `_test`, e.g. `seenad_zero_price_batch_test`.
2. Export:
   - `ZERO_PRICE_BATCH_PG_TESTS=1`
   - `ZERO_PRICE_BATCH_TEST_DATABASE_URL=postgresql+psycopg2://USER:PASS@HOST:5432/seenad_zero_price_batch_test`
3. Run only the integration module:
   `python -m unittest tests.test_zero_price_batch_registry_postgres -v`

Guards (fail-closed):

- Refuses `seenad_db` and `postgres` database names.
- Refuses any database name that does not end with `_test` unless listed in
  `ZERO_PRICE_BATCH_TEST_ALLOWED_DB_NAMES`.
- DDL from `tests/sql/zero_price_batch_integration_schema.sql` (test-only minimal tables,
  aligned with production `022` registry contract) runs **only** on the URL above — not
  production migration files that touch `car_zero_price`.

Unit tests in `tests/test_zero_price_batch_registry.py` remain mock-based and need no database.

## SB-01 benchmark beat

`ZERO_PRICE_BENCHMARK_BEAT_ENABLED` must stay **0** in production (`docker-compose.yml`).
This is unrelated to calculator zero-price ingestion.
