#!/usr/bin/env bash
set -euo pipefail

# Full backup script for Seenad project.
# Output artifact example: seenad_backup_2026_04_29.tar.gz

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATE_STAMP="$(date +%Y_%m_%d)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"

# Final archive location (downloadable path inside project).
BACKUP_OUTPUT_DIR="${PROJECT_ROOT}/backups"
ARCHIVE_NAME="seenad_backup_${DATE_STAMP}_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_OUTPUT_DIR}/${ARCHIVE_NAME}"
RAW_SQL_PATH="${BACKUP_OUTPUT_DIR}/db_dump.sql"

# Working directory for assembling backup content.
WORK_DIR="${PROJECT_ROOT}/.backup_tmp_${TIMESTAMP}"
DB_DIR="${WORK_DIR}/database"
CODE_DIR="${WORK_DIR}/project_code"
SECRETS_DIR="${WORK_DIR}/sensitive"
SSL_DIR="${SECRETS_DIR}/letsencrypt"

# Database config.
DB_SERVICE="${BACKUP_DB_SERVICE:-db}"
DB_NAME="${BACKUP_DB_NAME:-${POSTGRES_DB:-seenad_db}}"
DB_USER="${BACKUP_DB_USER:-${POSTGRES_USER:-postgres}}"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

mkdir -p "${BACKUP_OUTPUT_DIR}" "${DB_DIR}" "${CODE_DIR}" "${SECRETS_DIR}" "${SSL_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required for backup but was not found." >&2
  exit 1
fi

echo "[1/5] Backing up database '${DB_NAME}' from Docker service '${DB_SERVICE}' ..."
rm -f "${RAW_SQL_PATH}"

# User requested docker compose exec -t; if TTY is unavailable, fall back to -T.
if docker compose -f "${COMPOSE_FILE}" exec -t "${DB_SERVICE}" \
  pg_dump -U "${DB_USER}" "${DB_NAME}" > "${RAW_SQL_PATH}" 2>/dev/null; then
  echo "  - Database dump completed with docker compose exec -t."
elif docker compose -f "${COMPOSE_FILE}" exec -T "${DB_SERVICE}" \
  pg_dump -U "${DB_USER}" "${DB_NAME}" > "${RAW_SQL_PATH}"; then
  echo "  - Database dump completed with docker compose exec -T fallback."
else
  echo "ERROR: Could not dump database from Docker service '${DB_SERVICE}'." >&2
  exit 1
fi

cp -a "${RAW_SQL_PATH}" "${DB_DIR}/db_dump.sql"

echo "[2/5] Backing up project code (excluding node_modules/venv) ..."
tar -C "${PROJECT_ROOT}" \
  --exclude="./.git" \
  --exclude="./backups" \
  --exclude="./.backup_tmp_*" \
  --exclude="./frontend/node_modules" \
  --exclude="./**/node_modules" \
  --exclude="./venv" \
  --exclude="./.venv" \
  --exclude="./**/.venv" \
  --exclude="./**/venv" \
  -cf "${CODE_DIR}/project_code.tar" \
  .

echo "[3/5] Backing up .env files and SSL certificates ..."
# Copy all .env files under project (e.g. .env, frontend/.env, backend/.env).
mkdir -p "${SECRETS_DIR}/env_files"
if command -v rg >/dev/null 2>&1; then
  while IFS= read -r env_file; do
    [[ -z "${env_file}" ]] && continue
    dest_dir="${SECRETS_DIR}/env_files/$(dirname "${env_file}")"
    mkdir -p "${dest_dir}"
    cp -a "${PROJECT_ROOT}/${env_file}" "${dest_dir}/"
  done < <(cd "${PROJECT_ROOT}" && rg --files -g "**/.env" -g "**/.env.*")
fi

# SSL backup strategy:
# 1) Host path /etc/letsencrypt (if readable), 2) nginx container /etc/letsencrypt via docker cp.
SSL_COPIED=0
if [[ -d /etc/letsencrypt ]]; then
  if [[ -r /etc/letsencrypt ]]; then
    cp -a /etc/letsencrypt/. "${SSL_DIR}/"
    SSL_COPIED=1
    echo "  - SSL certificates copied from host path /etc/letsencrypt."
  else
    if [[ "${EUID}" -ne 0 ]]; then
      echo "  - /etc/letsencrypt exists but is not readable without sudo; skipping host SSL copy."
    else
      echo "  - /etc/letsencrypt exists but cannot be read; skipping host SSL copy."
    fi
  fi
fi

if [[ "${SSL_COPIED}" -eq 0 ]]; then
  SSL_CONTAINER_NAME="${BACKUP_SSL_CONTAINER:-}"
  SSL_SERVICE_NAME="${BACKUP_SSL_SERVICE:-nginx}"

  if [[ -n "${SSL_CONTAINER_NAME}" ]]; then
    :
  else
    service_container_id="$(docker compose -f "${COMPOSE_FILE}" ps -q "${SSL_SERVICE_NAME}" 2>/dev/null || true)"
    if [[ -n "${service_container_id}" ]]; then
      SSL_CONTAINER_NAME="$(docker inspect -f '{{.Name}}' "${service_container_id}" 2>/dev/null | sed 's#^/##')"
    fi
  fi

  if [[ -z "${SSL_CONTAINER_NAME}" ]]; then
    SSL_CONTAINER_NAME="$(docker ps --format '{{.Names}}' | rg -m1 'nginx|proxy' || true)"
  fi

  if [[ -n "${SSL_CONTAINER_NAME}" ]]; then
    if docker cp "${SSL_CONTAINER_NAME}:/etc/letsencrypt/." "${SSL_DIR}/" 2>/dev/null; then
      SSL_COPIED=1
      echo "  - SSL certificates copied from container '${SSL_CONTAINER_NAME}'."
    else
      echo "  - SSL container '${SSL_CONTAINER_NAME}' found, but /etc/letsencrypt copy failed."
    fi
  fi
fi

if [[ "${SSL_COPIED}" -eq 0 ]]; then
  echo "  - SSL certificates were not found or not accessible on host/container."
fi

echo "[4/5] Creating final compressed archive ..."
tar -C "${WORK_DIR}" -czf "${ARCHIVE_PATH}" .
rm -f "${RAW_SQL_PATH}"

echo "[5/5] Backup completed."
echo "Archive: ${ARCHIVE_PATH}"
echo "Size: $(du -h "${ARCHIVE_PATH}" | awk '{print $1}')"

