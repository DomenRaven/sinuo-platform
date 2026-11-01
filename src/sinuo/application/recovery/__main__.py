"""Local backup and restore command. It does not register an export job.

Run from the repository root:

    python -m sinuo.application.recovery backup --database <db> --objects <dir> --dest <dir>
    python -m sinuo.application.recovery restore --backup <dir> --dest <dir> --live-database <db>
"""

import argparse
import json
import sys
from pathlib import Path

from sinuo.application.recovery.snapshot import RecoveryError, backup_snapshot, restore_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sinuo.application.recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--objects", type=Path, required=True)
    backup.add_argument("--dest", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--dest", type=Path, required=True)
    restore.add_argument("--live-database", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_snapshot(
                source_database=args.database,
                objects_root=args.objects,
                backup_dir=args.dest,
            )
            payload = {
                "succeeded": True,
                "opened": True,
                "elapsed_seconds": result.elapsed_seconds,
                "manifest": result.manifest,
            }
            _write(payload)
            return 0
        restored = restore_snapshot(
            backup_dir=args.backup,
            destination=args.dest,
            live_database=args.live_database,
        )
    except (RecoveryError, ValueError, OSError) as error:
        failure = getattr(error, "failure", None)
        detail = getattr(error, "detail", None) or str(error)
        _write({"succeeded": False, "opened": False, "failure": failure, "detail": detail})
        return 1
    _write(restored.report)
    if not restored.succeeded:
        return 1
    if not restored.opened:
        return 2
    return 0


def _write(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    raise SystemExit(main())
