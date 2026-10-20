#!/usr/bin/env python3
"""Probe Hamrah Mechanic /carprice/ brand catalogue (counts + sample)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.hamrah_mechanic_carprice_service import run_hamrah_carprice_pipeline

OUTPUT = Path("data/hamrahmechanic_carprice_probe_sample.json")


def main() -> int:
    summary = run_hamrah_carprice_pipeline(
        output_path=Path("data/_probe_hamrah_carprice.jsonl"),
        dry_run=True,
        limit=30,
        sleep_s=0.02,
    )
    payload = summary.as_dict()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if summary.items_seen > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
