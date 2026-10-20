# Seenad crawler inventory

Quick map of existing scripts. Prefer extending these over starting from scratch.

## Probes → crawlers

| Source | Probe | Crawler | Default raw output |
|--------|-------|---------|-------------------|
| Behtarino dealers | (see repairshops probe pattern) | `crawl_behtarino.py` (`--all-cities` for non-Tehran) | `data/raw_behtarino_dealers.jsonl` |
| Behtarino repair shops | `probe_behtarino_repairshops.py` | `crawl_behtarino_repairshops.py` | `data/raw_behtarino_repairshops.jsonl` |
| Khodrochi repair shops | — | `crawl_khodrochi_repairshops.py` | `data/raw_khodrochi_repairshops.jsonl` |
| Bama dealers | — | `crawl_bama_dealers.py` | `data/raw_bama_dealers.jsonl` |
| Bama calculator refs | — | `crawl_bama_reference.py` | project JSON under repo/`data/` |
| Bama car specs | `probe_bama_specs.py` | `discover_bama_urls.py` + `run_bama_scraper.py` (sitemap + trim chips) | `data/raw_bama_car_specs.json` |
| IKCO dealers | — | `crawl_ikco.py` | `data/raw_ikco_dealers.json` |
| SAIPA | — | `crawl_saipa.py` | `data/raw_saipa_dealers.json` |
| Bahman Motor | `probe_bahman_motor.py` / `probe_bahman_pdf.py` | `crawl_bahman_motor.py` | `data/raw_bahman_motor_dealers.json` |
| Kerman Motor | `probe_kerman_motor.py` | `crawl_kerman_motor.py` | `data/raw_kerman_motor_dealers.json` |
| Arian Motor | `probe_arian_motor.py` / `probe_arian_pdf.py` | `crawl_arian_motor.py` / `crawl_arian_pdf.py` | `data/raw_arian_motor_dealers.json` |
| Bamco PDF | — | `crawl_bamco_pdf.py` | `data/raw_bamco_dealers.json` |
| Modiran Khodro | `probe_modiran_khodro.py` | `crawl_modiran_khodro.py` | `data/raw_modiran_khodro_dealers.json` |
| Ilia Auto | `probe_ilia.py` | `crawl_ilia.py` | `data/raw_ilia_dealers.json` |
| Diar Khodro | — | `crawl_diar_khodro.py` | `data/raw_diar_khodro.json` |
| Lucano | — | `crawl_lucano_dealerships.py` | `data/raw_lucano_dealerships.json` |
| Tigard | — | `crawl_tigard_dealerships.py` | `data/raw_tigard_dealerships.json` |
| Soroush Motor | — | `crawl_soroush_motor.py` | `data/soroush_motor_raw.json` |
| Balad dealerships | — | `crawl_balad_dealerships.py` | `data/balad_dealerships_raw.json` |
| Balad global search | — | `crawl_balad_global_search.py` | under `data/` |
| Akharin Khodro cars | `probe_akharinkhodro.py` | `crawl_akharinkhodro.py` | `data/raw_akharinkhodro_cars.json` |
| KhodroBank cars | `probe_khodrobank.py` | `crawl_khodrobank.py` | `data/raw_khodrobank_cars.jsonl` + `data/raw_khodrobank_cars.json` |
| Hamrah Mechanic articles | `probe_hamrahmechanic_articles.py` | `crawl_hamrahmechanic_articles.py` | `data/raw_hamrahmechanic_car_reviews.jsonl` |
| Khodro45 articles | `probe_khodro45_articles.py` | `crawl_khodro45_articles.py` | `data/raw_khodro45_review_compare.jsonl` |
| Sell-news / بخشنامه (khodro45 + khodrobank + car.ir + autonovin + megfa) | `probe_sell_news.py` | `crawl_sell_news.py` | Postgres `car_sell_news` / `car_sell_news_cars` (see `docs/sell_news.md`) |
| IranJib zero-km prices (domestic + imported) | `probe_iranjib.py` | `crawl_iranjib.py` | Postgres `iranjib_zero_price_scrape_snapshot` |
| Khodro45 pricing catalogue | `probe_khodro45_pricing.py` | `crawl_competitor_car_prices.py --source khodro45` | `data/raw_khodro45_pricing_catalog.jsonl` |
| Hamrah Mechanic carprice catalogue | `probe_hamrahmechanic_carprice.py` | `crawl_competitor_car_prices.py --source hamrah` | `data/raw_hamrahmechanic_carprice_catalog.jsonl` |
| StartMyCar guides + reviews (Toyota/SsangYong/VW/Mercedes/BMW/Mazda/Hyundai/Kia) | `probe_startmycar.py` | `crawl_startmycar.py` | `data/raw_startmycar_guides_reviews.jsonl` |
| Official OEM car galleries | `probe_car_image_sources.py` / `probe_car_gallery_poc.py` | `crawl_car_galleries.py` | `data/raw_car_galleries.jsonl` |
| OEM gallery binaries + import JSON | — | `download_car_galleries.py` | `data/car_profile_galleries/car_profile_galleries.json` |

## Pattern cheatsheet

- **SSR + JSON-LD listing → detail phones**: `crawl_behtarino.py`
- **Multi-city resume JSONL**: `crawl_behtarino_repairshops.py`
- **ASP.NET WebForms + postback**: `crawl_ikco.py`
- **PDF table extract**: `crawl_bamco_pdf.py`, `crawl_arian_pdf.py`
- **Map/API research**: `research_maps.py` (Balad/Neshan probes)
- **WordPress REST (child categories OR)**: `crawl_akharinkhodro.py`

## Docker reminders

- Compose service: `backend` → container name `seenad_backend`
- Prefer `docker compose exec -T backend ...` for probes; `docker exec -i seenad_backend ...` is common for long crawls
- Working directory inside container is the app root mounted with `scripts/` and `data/`
