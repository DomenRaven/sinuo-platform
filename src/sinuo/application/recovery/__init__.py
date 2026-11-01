"""Isolated backup and restore. This package does not register an export job."""

from sinuo.application.recovery.snapshot import (
    FAILURE_CORRUPT,
    FAILURE_INCOMPATIBLE_VERSION,
    FAILURE_MISSING_ORIGINAL,
    BackupResult,
    RecoveryError,
    RestoreResult,
    backup_snapshot,
    restore_snapshot,
)

__all__ = [
    "FAILURE_CORRUPT",
    "FAILURE_INCOMPATIBLE_VERSION",
    "FAILURE_MISSING_ORIGINAL",
    "BackupResult",
    "RecoveryError",
    "RestoreResult",
    "backup_snapshot",
    "restore_snapshot",
]
