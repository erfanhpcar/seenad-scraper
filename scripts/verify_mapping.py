from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal


def verify_single(provider_name: str, source_string: str) -> int:
    query = text(
        """
        UPDATE mapping_provider_data
        SET is_verified = TRUE,
            updated_at = NOW()
        WHERE provider_name = :provider_name
          AND lower(btrim(source_string)) = lower(btrim(:source_string))
        """
    )
    with SessionLocal() as session:
        result = session.execute(
            query,
            {"provider_name": provider_name, "source_string": source_string},
        )
        session.commit()
        return int(result.rowcount or 0)


def apply_fix(provider_name: str, source_string: str, target_reference_id: int, verify: bool) -> int:
    query = text(
        """
        UPDATE mapping_provider_data
        SET target_reference_id = :target_reference_id,
            is_verified = :is_verified,
            updated_at = NOW()
        WHERE provider_name = :provider_name
          AND lower(btrim(source_string)) = lower(btrim(:source_string))
        """
    )
    with SessionLocal() as session:
        result = session.execute(
            query,
            {
                "provider_name": provider_name,
                "source_string": source_string,
                "target_reference_id": target_reference_id,
                "is_verified": verify,
            },
        )
        session.commit()
        return int(result.rowcount or 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify/fix mapping rows by source_string. Supports single and batch JSON modes."
    )
    parser.add_argument("source_string", nargs="?", help="Source string stored in mapping_provider_data")
    parser.add_argument("--provider", default="divar")
    parser.add_argument(
        "--json-file",
        help=(
            "Batch mode JSON file. Accepted item formats: "
            "{source_string}, "
            "{source_string, target_reference_id}, "
            "{source_string, action=verify|fix, target_reference_id}"
        ),
    )
    args = parser.parse_args()

    if args.json_file:
        payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Batch JSON must be a list of objects.")
        total_changed = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            source_string = str(item.get("source_string") or "").strip()
            if not source_string:
                continue
            action = str(item.get("action") or "").strip().lower()
            if not action:
                action = "fix" if item.get("target_reference_id") is not None else "verify"
            if action == "fix":
                target_ref_id = item.get("target_reference_id")
                if target_ref_id is None:
                    raise ValueError(f"target_reference_id is required for fix action: {source_string}")
                verify = bool(item.get("is_verified", True))
                changed = apply_fix(
                    provider_name=args.provider,
                    source_string=source_string,
                    target_reference_id=int(target_ref_id),
                    verify=verify,
                )
                print(
                    f"[fix] source={source_string!r} target_reference_id={int(target_ref_id)} "
                    f"is_verified={verify} changed={changed}"
                )
            else:
                changed = verify_single(provider_name=args.provider, source_string=source_string)
                print(f"[verify] source={source_string!r} changed={changed}")
            total_changed += changed
        print(f"Batch completed. Total changed rows: {total_changed}")
        return

    if not args.source_string:
        raise ValueError("source_string is required in single mode.")
    changed = verify_single(provider_name=args.provider, source_string=args.source_string)
    if changed <= 0:
        print("No row matched. Nothing updated.")
        return
    print(f"Verified {changed} row(s) for source_string={args.source_string!r} provider={args.provider!r}.")


if __name__ == "__main__":
    main()
