#!/usr/bin/env bash
# Start benchmark_worker when production DB URL is configured (consumes scheduled_tasks).
# SB-01: legacy scraper benchmark writer is isolated — do not start without explicit compat opt-in.
set -euo pipefail
cd /var/www/seenad-scraper

if [[ "${BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED:-0}" != "1" ]]; then
  echo "SB-01: benchmark_worker not started (set BENCHMARK_LEGACY_WRITER_COMPAT_VERIFIED=1 after schema audit)." >&2
  exit 0
fi

SECRET="secrets/benchmark_database_url"
if [[ ! -f "$SECRET" ]]; then
  exit 0
fi

export BENCHMARK_DATABASE_URL="$(tr -d '[:space:]' < "$SECRET")"
if [[ -z "$BENCHMARK_DATABASE_URL" ]]; then
  exit 0
fi

if docker ps --format '{{.Names}}' | grep -qx seenad_benchmark_worker; then
  exit 0
fi

docker compose -p project --profile benchmark up -d benchmark_worker
docker compose -p project ps benchmark_worker
