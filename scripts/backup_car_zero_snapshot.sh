#!/usr/bin/env bash
# Backup car_zero_price and car_zero_price_scrape_snapshot before a full hierarchy crawl.
#
# Run from the project root (host). Defaults: container seenad_db, output ./backups/
#
# Apply migration 012 before first --full-crawl (paths are inside the db container):
#   docker exec -i seenad_db sh -c \
#     'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
#        -f /var/www/seenad/postgres/migrations/012_car_zero_price_scrape_snapshot.sql'
#
set -euo pipefail

CONTAINER="${SEENAD_DB_CONTAINER:-seenad_db}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$OUT_DIR"
OUT_FILE="${OUT_DIR}/seenad_car_zero_${STAMP}.sql.gz"

docker exec "$CONTAINER" sh -c '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --data-only \
    -t car_zero_price \
    -t car_zero_price_scrape_snapshot
' | gzip -c >"$OUT_FILE"

echo "Wrote $OUT_FILE"
