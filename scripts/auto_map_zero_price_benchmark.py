#!/usr/bin/env python3
"""One-shot automap for zero-price benchmark competitor sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from src.zero_price_benchmark.automap import run_automap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Max benchmark cars (smoke).")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        summary = run_automap(session, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
