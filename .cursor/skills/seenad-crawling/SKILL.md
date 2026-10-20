---
name: seenad-crawling
description: >-
  Build and run Seenad scrapers: probe a source, write a production crawler,
  write raw JSON/JSONL under data/, and execute via Docker backend. Use when
  the user mentions crawl, کراول, scraper, probe, dealership/repairshop
  extraction, Bama/Behtarino/Balad/IKCO sources, or scripts under scripts/crawl_*.py.
---

# Seenad Crawling

Project: `seenad-scraper`. Follow existing crawlers; do not invent a parallel stack.

## Workflow (mandatory)

```
Task Progress:
- [ ] 1. Reuse: check scripts/crawl_*.py and scripts/probe_*.py for the same source
- [ ] 2. Probe: scripts/probe_<source>.py — map fields, APIs, anti-bot, pagination
- [ ] 3. Smoke crawl: limited pages/records before full run
- [ ] 4. Production crawl: scripts/crawl_<source>.py → data/raw_<name>.json(l)
- [ ] 5. Verify: count records, spot-check phones/coords, confirm UTF-8 Persian
```

**Never skip probe** for a new source. Prefer extending an existing crawler over a new file.

## Naming & paths

| Kind | Path |
|------|------|
| Probe | `scripts/probe_<source>.py` |
| Crawler | `scripts/crawl_<source>.py` |
| Raw output | `data/raw_<source>_<entity>.json` or `.jsonl` |
| Probe sample | `data/<source>_probe_sample.json` (or `_probe_raw.html`) |
| Logs | `data/<source>_crawl.log` when long-running |

- JSONL when streaming/resumeable (directories like Behtarino).
- Pretty JSON (`indent=2`) when the full set is small and rewritten atomically.
- Always `ensure_ascii=False` and UTF-8.

## Stack defaults

- HTTP: `requests.Session` + browser `User-Agent` (rotate only if needed).
- HTML: `beautifulsoup4`.
- PDF tables: `pdfplumber` (see `crawl_arian_pdf.py`, `crawl_bamco_pdf.py`).
- Prefer embedded JSON / JSON-LD / Next/Nuxt payloads over brittle DOM.
- Playwright/Selenium only if HTTP+BS4 cannot pass anti-bot or heavy JS — justify first.
- Dependencies must already be in `requirements.txt` (or add them explicitly).

## Crawler CLI conventions

Every production crawler should support:

- `--output` (default under `data/raw_...`)
- `-v` / `--verbose`
- smoke limits: `--limit`, `--limit-pages`, `--max-pages`, or equivalent
- `--resume` when appending JSONL
- Module docstring with Usage examples (host + Docker)

Include polite delays (`random.uniform` between requests). Detect common anti-bot markers (Cloudflare / Arvan / "just a moment") and fail clearly.

Normalize Persian where useful: Arabic `ي/ك` → `ی/ک`, Persian digits → ASCII, collapse whitespace.

## Record shape

Keep extracted fields flat and import-friendly; stash leftovers under `raw` / `raw_payload`.

Typical dealer/shop fields: `name`, `address`, `city`, `province`, `phones`, `lat`, `lng`, `url`/`profile_url`, `source`, `crawled_at` (UTC ISO).

Do not write test/smoke outputs over the canonical `data/raw_*.json(l)` — use `*_test` / `*_smoke` / `*_preview` suffixes.

## Run via Docker

Container: `seenad_backend` (compose service `backend`).

```bash
# Probe
docker compose exec -T backend python3 scripts/probe_<source>.py

# Smoke
docker exec -i seenad_backend python3 scripts/crawl_<source>.py --limit-pages 2 -v

# Full / resume
docker exec -i seenad_backend python3 scripts/crawl_<source>.py --resume
```

Run from repo root so `data/` paths resolve inside the container volume.

## Quality bar before calling done

- Docstring documents strategy and Usage.
- Smoke run succeeds and prints/log counts.
- Output lands under `data/` (not `/tmp`, not repo root).
- Empty result set → non-zero exit or loud warning.
- No secrets committed; no scraping credentials hard-coded.

## References

- Existing script inventory: [reference.md](reference.md)
- Prefer copying structure from a close sibling (e.g. Behtarino listing+detail → `crawl_behtarino.py` / `crawl_behtarino_repairshops.py`).
