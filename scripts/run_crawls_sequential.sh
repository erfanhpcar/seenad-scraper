#!/usr/bin/env bash
# Sequential production crawls (one after another) via seenad_backend.
# Logs: data/crawl_batch_*.log and per-crawler logs.

set -u
cd /var/www/seenad-scraper || exit 1
mkdir -p data

BATCH_LOG="data/crawl_batch_sequential.log"
PID_FILE="data/crawl_batch_sequential.pid"
echo $$ > "$PID_FILE"

log() {
  echo "[$(date -Is)] $*" | tee -a "$BATCH_LOG"
}

run_one() {
  local name="$1"
  shift
  local logfile="data/${name}_crawl.log"
  log "START ${name}"
  log "CMD: docker exec seenad_backend $*"
  # shellcheck disable=SC2068
  if docker exec seenad_backend "$@" >>"$logfile" 2>&1; then
    log "OK ${name} (exit 0) → ${logfile}"
    return 0
  else
    local code=$?
    log "FAIL ${name} (exit ${code}) → ${logfile}"
    return "$code"
  fi
}

log "==== sequential crawl batch begin ===="

# 1) آخرین خودرو — مشخصات (REST, JSON)
run_one "akharinkhodro" python3 scripts/crawl_akharinkhodro.py -v

# 2) همراه مکانیک — مقالات (REST, JSONL)
run_one "hamrahmechanic_articles" python3 scripts/crawl_hamrahmechanic_articles.py --resume -v

# 3) خودرو45 — مقالات (REST, JSONL)
run_one "khodro45_articles" python3 scripts/crawl_khodro45_articles.py --resume -v

# 4) اسب بخار — مشخصات (HTML, JSONL)
run_one "asbebokhar" python3 scripts/crawl_asbebokhar.py --resume -v

# 5) خودروبانک — مشخصات (HTML سنگین, JSONL)
run_one "khodrobank" python3 scripts/crawl_khodrobank.py --resume -v

log "==== sequential crawl batch end ===="
rm -f "$PID_FILE"
