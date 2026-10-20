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

## Batch completion (`zero_price_scrape_batch`)

Snapshot tables (`car_zero_price_scrape_snapshot`, `iranjib_zero_price_scrape_snapshot`)
commit incrementally every 400 rows. Without a completion marker, **seenad-main** could treat
a partial `batch_id` as latest.

Migration: `postgres/migrations/022_zero_price_scrape_batch.sql`

| status        | Meaning |
|---------------|---------|
| `in_progress` | Crawl running; ignore snapshot rows for this `batch_id`. |
| `completed`   | Full crawl; safe to consume. |
| `failed`      | Aborted; snapshot rows for this `batch_id` are deleted. |

### seenad-main rollout order

1. Apply `022_zero_price_scrape_batch.sql` on the scraper DB (no data backfill required).
2. Deploy scraper with registry writes (this repo).
3. Deploy seenad-main change: resolve latest batch via `zero_price_scrape_batch` where
   `status = 'completed'` per `source`, ordered by `completed_at DESC`.
4. Until step 3 ships, main may keep the old `MAX(created_at)` heuristic; new incomplete
   batches are marked `in_progress`/`failed` and should not be selected once main filters
   on `completed`.

Example (Bama):

```sql
SELECT batch_id
FROM zero_price_scrape_batch
WHERE source = 'bama' AND status = 'completed'
ORDER BY completed_at DESC
LIMIT 1;
```

Celery retries start a **new** `batch_id`; failed partial batches are cleaned up and marked
`failed`.

## SB-01 benchmark beat

`ZERO_PRICE_BENCHMARK_BEAT_ENABLED` must stay **0** in production (`docker-compose.yml`).
This is unrelated to calculator zero-price ingestion.
