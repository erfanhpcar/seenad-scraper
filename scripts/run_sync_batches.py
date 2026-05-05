from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base, SessionLocal, engine
from app.models import CarEntity

DEFAULT_BATCH_SIZE = 50
DEFAULT_COOLDOWN_SECONDS = 15 * 60
DEFAULT_TARGET_COUNT = 500
DEFAULT_PRIORITY_BRANDS = "jac,peugeot,mvm,chery"
SYNC_SCRIPT_PATH = Path(__file__).resolve().parent / "sync_car_reference_from_bama.py"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bama car-reference sync in safe batches.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--target-count", type=int, default=DEFAULT_TARGET_COUNT)
    parser.add_argument("--max-batches", type=int, default=0, help="0 means unlimited.")
    parser.add_argument("--priority-brands", type=str, default=DEFAULT_PRIORITY_BRANDS)
    parser.add_argument("--python-bin", type=str, default=sys.executable)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the first batch command and exit without running.",
    )
    return parser


def _get_reference_count() -> int:
    db = SessionLocal()
    try:
        return db.scalar(select(func.count(CarEntity.id))) or 0
    finally:
        db.close()


def _format_seconds(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _run_one_batch(args: argparse.Namespace) -> None:
    command = [
        args.python_bin,
        str(SYNC_SCRIPT_PATH),
        "--max-ads",
        str(args.batch_size),
        "--priority-brands",
        args.priority_brands,
    ]
    subprocess.run(command, check=True)


def main() -> None:
    args = _build_parser().parse_args()
    Base.metadata.create_all(bind=engine, tables=[CarEntity.__table__])

    start_count = _get_reference_count()
    print(f"Starting count in car_entities: {start_count}")

    if args.dry_run:
        print("Dry-run mode, first batch command:")
        print(
            f"{args.python_bin} {SYNC_SCRIPT_PATH} --max-ads {args.batch_size} "
            f'--priority-brands "{args.priority_brands}"'
        )
        return

    batch_number = 0
    while True:
        current_count = _get_reference_count()
        if current_count >= args.target_count:
            print(f"Target reached: {current_count}/{args.target_count}. Stopping.")
            break
        if args.max_batches > 0 and batch_number >= args.max_batches:
            print(f"Reached max-batches={args.max_batches}. Stopping.")
            break

        batch_number += 1
        before_count = current_count
        print(
            f"\n=== Batch {batch_number} start | current_count={before_count} "
            f"| target={args.target_count} ==="
        )

        _run_one_batch(args)

        after_count = _get_reference_count()
        inserted_in_batch = max(after_count - before_count, 0)
        print(
            f"Batch {batch_number} report: new_records={inserted_in_batch} | "
            f"total_records={after_count}"
        )

        if after_count >= args.target_count:
            print(f"Target reached: {after_count}/{args.target_count}. Stopping.")
            break

        if args.max_batches > 0 and batch_number >= args.max_batches:
            print(f"Reached max-batches={args.max_batches}. Stopping.")
            break

        print(f"Next batch in: {_format_seconds(args.cooldown_seconds)}")
        time.sleep(args.cooldown_seconds)


if __name__ == "__main__":
    main()
