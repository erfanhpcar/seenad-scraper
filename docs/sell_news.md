# Sell-news / بخشنامه + USD free-market rate (Seenad 11:15 pickup)

Seenad reads **Postgres only** at 11:15 Asia/Tehran. The scraper does not expose an API for this.

| Job | Table | 11:00 Tehran | 16:00 Tehran | 18:00 Tehran | Must finish |
|--|--|--|--|--|--|
| Free-market USD | `usd_irr_rate` (`instrument_key=price_dollar_rl`) | yes | — | **yes (kept)** | 11:10 (morning) |
| Sell circulars | `car_sell_news` + `car_sell_news_cars` | yes | yes | — | 11:10 (morning) |

Sources: `khodro45`, `khodrobank`, `car.ir`, `autonovin` (order.autonovin.ir API + optional PDF), `megfa` (`salecars.ir/announcements`).

`scrape_batch_id` for news is Jalali `sell-news-YYYY-MM-DD` (e.g. `sell-news-1405-05-24`).
USD row key is `(as_of_date=today Tehran, source=tgju)`.

## Cron (recommended: sequential wrapper)

USD first, then sell-news:

```cron
CRON_TZ=Asia/Tehran
0 11 * * * docker exec seenad_backend python3 scripts/run_seenad_1115_window.py >> /var/www/seenad-scraper/data/seenad_1115_window.log 2>&1
0 16 * * * docker exec seenad_backend python3 scripts/crawl_sell_news.py >> /var/www/seenad-scraper/data/sell_news_cron.log 2>&1
```

Equivalent two-line morning form (same minute; order not guaranteed):

```cron
CRON_TZ=Asia/Tehran
0 11 * * * docker exec seenad_backend python3 scripts/crawl_usd_irr_rate.py >> /var/www/seenad-scraper/data/usd_irr_rate_cron.log 2>&1
0 11 * * * docker exec seenad_backend python3 scripts/crawl_sell_news.py >> /var/www/seenad-scraper/data/sell_news_cron.log 2>&1
0 16 * * * docker exec seenad_backend python3 scripts/crawl_sell_news.py >> /var/www/seenad-scraper/data/sell_news_cron.log 2>&1
```

Use **either** this host cron **or** Celery Beat, not both.

## Celery Beat (UTC)

| Slot | UTC | Tehran |
|--|--|--|
| Morning USD + sell-news | `07:30` | 11:00 |
| Afternoon sell-news refresh | `12:30` | 16:00 |
| Afternoon USD refresh | `14:30` | 18:00 |

Env: `USD_IRR_RATE_BEAT_HOURS_UTC=7,14` and `SELL_NEWS_BEAT_HOURS_UTC=7,12` / `MINUTE=30`.
The 18:00 USD job is still enabled.

If TGJU has no today-dated print by 11:00, the last valid rate is written onto `as_of_date=today` (`carried_forward=true`). If nothing can be written, the log says today's USD row was **not** created.

## Commands

```bash
docker exec -i seenad_backend python3 scripts/crawl_usd_irr_rate.py -v
docker exec -i seenad_backend python3 scripts/crawl_sell_news.py -v
# both, USD then news:
docker exec -i seenad_backend python3 scripts/run_seenad_1115_window.py
```

## Seenad 11:15 SELECT

```sql
-- 1) today's free-market USD
SELECT as_of_date, rate_irr, rate_toman, instrument_key, quoted_at, updated_at
FROM usd_irr_rate
WHERE source = 'tgju'
  AND instrument_key = 'price_dollar_rl'
  AND as_of_date = ((now() AT TIME ZONE 'Asia/Tehran')::date);

-- 2) today's sell-news batch
SELECT n.*, c.*
FROM car_sell_news n
LEFT JOIN car_sell_news_cars c ON c.news_id = n.id
WHERE n.scrape_batch_id = :today_batch   -- e.g. sell-news-1405-05-24
ORDER BY n.published_at DESC, c.row_index;
```
