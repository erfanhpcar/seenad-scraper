from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.external_inspector import ExternalDbInspector


def main() -> None:
    # Allow quick execution outside Docker:
    # EXT_DB_HOST=... EXT_DB_USER=... EXT_DB_PASS=... EXT_DB_NAME=... python3 scripts/get_tables.py
    required_vars = ("EXT_DB_HOST", "EXT_DB_USER", "EXT_DB_PASS", "EXT_DB_NAME")
    missing = [key for key in required_vars if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    inspector = ExternalDbInspector()
    tables = inspector.list_all_tables()
    print(f"Total tables: {len(tables)}")


if __name__ == "__main__":
    main()
