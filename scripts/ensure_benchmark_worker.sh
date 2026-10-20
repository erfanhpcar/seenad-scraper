#!/usr/bin/env bash
# Start benchmark_worker when production DB URL is configured (consumes scheduled_tasks).
set -euo pipefail
cd /var/www/seenad-scraper

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
