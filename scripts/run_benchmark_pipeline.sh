#!/usr/bin/env bash
# Zero-price benchmark: automap + full run against production Postgres.
# SB-01: blocked unless legacy writer compatibility has been explicitly verified.
set -euo pipefail
cd /var/www/seenad-scraper

if [[ "${BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED:-0}" != "1" ]]; then
  echo "ERROR: SB-01 legacy benchmark writer is isolated. seenad-main owns benchmark writes." >&2
  echo "Set BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED=1 only after main-app schema compatibility is verified." >&2
  exit 1
fi

load_url() {
  if [[ -f secrets/benchmark_database_url ]]; then
    BENCHMARK_DATABASE_URL="$(tr -d '[:space:]' < secrets/benchmark_database_url)"
  fi
  BENCHMARK_DATABASE_URL="${BENCHMARK_DATABASE_URL:-}"
  if [[ -z "$BENCHMARK_DATABASE_URL" ]]; then
    echo "ERROR: Put SQLAlchemy URL in secrets/benchmark_database_url or export BENCHMARK_DATABASE_URL" >&2
    echo "Example: postgresql+psycopg2://USER:PASS@HOST:5432/DBNAME" >&2
    exit 1
  fi
  export BENCHMARK_DATABASE_URL
}

preflight() {
  echo "== HM catalogue refresh (for benchmark HM prices) =="
  docker exec seenad_backend python3 scripts/crawl_competitor_car_prices.py --source hamrah -v
  echo "== DB check =="
  docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_backend python3 scripts/check_benchmark_db.py
}

up_workers() {
  echo "== benchmark_worker + beat =="
  docker compose -p project --profile benchmark up -d benchmark_worker
  docker compose -p project restart beat
  docker compose -p project ps benchmark_worker beat
}

import_watchlist() {
  echo "== watchlist import (if empty) =="
  set +e
  docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_backend python3 - <<'PY'
import os
from sqlalchemy import create_engine, text
url = os.environ["DATABASE_URL"]
e = create_engine(url)
with e.connect() as c:
    n = c.execute(text("SELECT count(*) FROM benchmark_car WHERE is_active IS TRUE")).scalar_one()
print("active_before", n)
if n == 0:
    raise SystemExit(42)
PY
  rc=$?
  set -e
  if [[ "$rc" -eq 42 ]]; then
    docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_backend \
      python3 -m scripts.import_zero_price_benchmark_watchlist \
      --xlsx "tmp/SeenAd_Top_50_Setoudeh_Priority (1).xlsx" --replace
  fi
}

automap() {
  echo "== automap (best-effort) =="
  docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_backend \
    python3 -m scripts.auto_map_zero_price_benchmark
}

run_benchmark() {
  echo "== celery benchmark run (long) =="
  docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_benchmark_worker \
    celery -A app.celery_app:celery_app call tasks.run_daily_zero_price_benchmark
}

verify() {
  echo "== latest run summary =="
  docker exec -e DATABASE_URL="$BENCHMARK_DATABASE_URL" seenad_backend python3 - <<'PY'
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    row = c.execute(text(
        "SELECT id, status, expected_count, processed_count FROM benchmark_run ORDER BY id DESC LIMIT 1"
    )).mappings().first()
    print(dict(row) if row else "no runs")
    if row:
        rid = row["id"]
        stats = c.execute(text(
            "SELECT source, status, count(*) FROM benchmark_observation WHERE run_id=:r GROUP BY 1,2 ORDER BY 1,2"
        ), {"r": rid}).all()
        for s in stats:
            print(s)
PY
}

load_url
CMD="${1:-all}"
case "$CMD" in
  preflight) preflight ;;
  up) up_workers ;;
  automap) automap ;;
  run) run_benchmark ;;
  verify) verify ;;
  import) import_watchlist ;;
  all)
    preflight
    up_workers
    import_watchlist
    automap
    run_benchmark
    verify
    ;;
  *)
    echo "Usage: $0 {preflight|up|import|automap|run|verify|all}"
    exit 1
    ;;
esac
