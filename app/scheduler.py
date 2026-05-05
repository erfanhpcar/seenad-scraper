from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone

from app.tasks.ingestion import ingestion_task


def _interval_seconds() -> int:
    # Default: every 6 hours.
    raw = os.getenv("NORMALIZER_INTERVAL_SECONDS", str(6 * 60 * 60))
    try:
        value = int(raw)
    except ValueError:
        value = 6 * 60 * 60
    return max(value, 60)


def main() -> None:
    interval = _interval_seconds()

    while True:
        started_at = datetime.now(timezone.utc).isoformat()
        print(f"[scheduler] run started at {started_at}")
        try:
            ingestion_task.delay()
            print(
                "[scheduler] ingestion_task scheduled "
                "(normalization → divar_mapping_relink → listings_clean chain follows ingest inside worker)"
            )
        except Exception:
            print("[scheduler] run failed")
            traceback.print_exc()

        print(f"[scheduler] sleeping for {interval} seconds")
        time.sleep(interval)


if __name__ == "__main__":
    main()
