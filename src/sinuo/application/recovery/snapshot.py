"""Backup a SQLite project and restore it into an empty directory.

The live database is read with the SQLite online backup API so the WAL is part
of the snapshot. Restore writes only the caller-supplied directory. It does not
open the source database for writing, and it does not use ``job_kind='export'``.
"""

import hashlib
import json
import platform
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sinuo.application.retrieval.search import rebuild_evidence_index
from sinuo.infrastructure.assets.objects import object_relative, original_key
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.models import SCHEMA_REVISION

FORMAT_VERSION = 1
FAILURE_CORRUPT = "corrupt"
FAILURE_MISSING_ORIGINAL = "missing_original"
FAILURE_INCOMPATIBLE_VERSION = "incompatible_version"
DETAIL_CORRUPT = "备份已损坏"
DETAIL_MISSING_ORIGINAL = "原件缺失"
DETAIL_INCOMPATIBLE_VERSION = "版本不兼容"

_DATABASE_NAME = "database.sqlite"
_RESTORED_NAME = "sinuo.db"
_MANIFEST_NAME = "manifest.json"
_REPORT_NAME = "recovery_report.json"
_ORIGINALS_DIR = "originals"
_DETAILS = {
    FAILURE_CORRUPT: DETAIL_CORRUPT,
    FAILURE_MISSING_ORIGINAL: DETAIL_MISSING_ORIGINAL,
    FAILURE_INCOMPATIBLE_VERSION: DETAIL_INCOMPATIBLE_VERSION,
}
_PAGE_SQL = """
SELECT id, page_id, version_no, parent_version_id, content_hash, project_revision
FROM page_versions ORDER BY id
"""
_BRANCH_SQL = """
SELECT id, source_version_id, target_version_id, kind, project_revision
FROM page_edges ORDER BY id
"""
_DECISION_SQL = """
SELECT id, body, reason, page_version_id, artifact_version_id, stage_id, project_revision
FROM decisions ORDER BY id
"""
_ARTIFACT_SQL = """
SELECT id, artifact_id, content_hash, size_bytes, object_key
FROM artifact_versions ORDER BY id
"""
_GRANT_SQL = """
SELECT id, project_id, account_id, action, scope_type, status, revoked_at,
       stage_id, page_id, page_version_id, artifact_id, artifact_version_id,
       evidence_anchor_id
FROM object_grants ORDER BY id
"""
_MEMBERSHIP_SQL = """
SELECT id, project_id, account_id, status, revoked_at
FROM memberships ORDER BY id
"""


class RecoveryError(Exception):
    """A backup could not be published. ``failure`` is not an API error code."""

    def __init__(self, failure: str, detail: str) -> None:
        self.failure = failure
        self.detail = detail
        super().__init__(detail)


class _Rejected(Exception):
    def __init__(self, failure: str) -> None:
        self.failure = failure
        super().__init__(_DETAILS[failure])


@dataclass(frozen=True)
class BackupResult:
    backup_dir: Path
    elapsed_seconds: float
    manifest: dict[str, Any]


@dataclass(frozen=True)
class RestoreResult:
    succeeded: bool
    failure: str | None
    opened: bool
    elapsed_seconds: float
    report: dict[str, Any]


@dataclass(frozen=True)
class _LiveDeletion:
    tombstones: tuple[sqlite3.Row, ...]
    audits: dict[str, sqlite3.Row]
    revoked_grants: dict[str, float]
    revoked_memberships: dict[str, float]
    access_epochs: dict[str, int]


def backup_snapshot(
    *,
    source_database: Path,
    objects_root: Path,
    backup_dir: Path,
) -> BackupResult:
    """Write a consistent snapshot, original bytes, and a manifest.

    ``backup_dir`` must not already exist. The source database is not updated.
    """

    started = time.perf_counter()
    source = _require_file(source_database, "source database")
    objects = _require_dir(objects_root, "objects root")
    _require_absent(backup_dir, "backup directory")
    staging = backup_dir.parent / f"{backup_dir.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        snapshot = staging / _DATABASE_NAME
        journal = _snapshot_database(source, snapshot)
        size, digest = _file_sha256(snapshot)
        if not _lone_ok(snapshot):
            raise RecoveryError(FAILURE_CORRUPT, DETAIL_CORRUPT)
        with _read_connection(snapshot) as connection:
            originals, non_store = _pack_originals(connection, objects, staging / _ORIGINALS_DIR)
            revisions, epochs = _revision_catalog(connection)
            schema_revision = _schema_revision(connection)
        now = time.time()
        elapsed = time.perf_counter() - started
        manifest = {
            "format_version": FORMAT_VERSION,
            "schema_revision": schema_revision,
            "backup_method": "sqlite_online_backup",
            "source_journal_mode": journal,
            "database_sha256": digest,
            "database_bytes": size,
            "backed_up_at": now,
            "elapsed_seconds": elapsed,
            "project_revisions": revisions,
            "tombstone_epoch": epochs,
            "originals": originals,
            "non_store_object_keys": non_store,
            "derivatives_omitted": ["previews", "evidence_index"],
            "environment": _environment(),
        }
        _write_json(staging / _MANIFEST_NAME, manifest)
        _verify_packed(staging, manifest)
    except _Rejected as rejected:
        shutil.rmtree(staging, ignore_errors=True)
        raise RecoveryError(rejected.failure, _DETAILS[rejected.failure]) from rejected
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(backup_dir)
    manifest["elapsed_seconds"] = time.perf_counter() - started
    _write_json(backup_dir / _MANIFEST_NAME, manifest)
    return BackupResult(backup_dir, manifest["elapsed_seconds"], manifest)


def restore_snapshot(
    *,
    backup_dir: Path,
    destination: Path,
    live_database: Path | None,
) -> RestoreResult:
    """Restore into ``destination`` and leave the source database untouched.

    A missing live deletion list keeps the copy offline. Previews are not
    restored. The evidence index is rebuilt, then tombstoned rows are removed
    from that index. Neither derivative is reported as restored from the backup.
    """

    started = time.perf_counter()
    try:
        if not backup_dir.is_dir():
            return _failure(FAILURE_CORRUPT, started)
        manifest = _read_manifest(backup_dir / _MANIFEST_NAME)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _failure(FAILURE_CORRUPT, started)
    classified = _classify_manifest(manifest)
    if classified is not None:
        return _failure(classified, started)
    try:
        _reject_destination(backup_dir, destination, live_database)
    except ValueError:
        raise
    try:
        _package_problem(backup_dir, manifest)
    except _Rejected as rejected:
        return _failure(rejected.failure, started)
    live, live_state = _read_live(live_database)
    staging = destination.parent / f"{destination.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        snapshot = backup_dir / _DATABASE_NAME
        shutil.copyfile(snapshot, staging / _RESTORED_NAME)
        withheld, copied = _place_originals(backup_dir, staging, manifest, live)
        post = _post_process(staging / _RESTORED_NAME, live)
        if not _integrity_ok(staging / _RESTORED_NAME):
            raise _Rejected(FAILURE_CORRUPT)
        _match_checks(snapshot, staging / _RESTORED_NAME, live, manifest, withheld)
    except _Rejected as rejected:
        shutil.rmtree(staging, ignore_errors=True)
        return _failure(rejected.failure, started)
    except _CheckFailed as failed:
        shutil.rmtree(staging, ignore_errors=True)
        return _incomplete(started, failed.check)
    except (sqlite3.Error, SQLAlchemyError, OSError, PathEscape) as error:
        shutil.rmtree(staging, ignore_errors=True)
        return _incomplete(started, str(error))
    opened = live_state == "applied" and post["deletion_applied"]
    report = _success_report(
        manifest=manifest,
        elapsed=time.perf_counter() - started,
        opened=opened,
        live_state=live_state if post["deletion_applied"] else "not_applied",
        post=post,
        withheld=withheld,
        copied=copied,
    )
    _write_json(staging / _REPORT_NAME, report)
    staging.rename(destination)
    return RestoreResult(True, None, opened, report["elapsed_seconds"], report)


def _snapshot_database(source: Path, dest_file: Path) -> str:
    """Fold the live WAL into one snapshot file via the online backup API."""

    journal = _journal_mode(source)
    last_error: sqlite3.Error | None = None
    for opener in (_open_read, _open_query_only):
        if dest_file.exists():
            dest_file.unlink()
        src = opener(source)
        dst = sqlite3.connect(dest_file)
        try:
            src.execute("PRAGMA busy_timeout=5000")
            dst.execute("PRAGMA busy_timeout=5000")
            src.backup(dst)
            mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode is None or str(mode[0]).lower() != "delete":
                raise sqlite3.OperationalError("snapshot journal was not folded")
            dst.commit()
        except sqlite3.OperationalError as error:
            last_error = error
            continue
        finally:
            dst.close()
            src.close()
        if _lone_ok(dest_file):
            return journal
        last_error = sqlite3.OperationalError("snapshot is not self-contained")
    raise RecoveryError(FAILURE_CORRUPT, DETAIL_CORRUPT) from last_error


def _pack_originals(
    connection: sqlite3.Connection,
    objects_root: Path,
    originals_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    originals_root.mkdir(parents=True, exist_ok=True)
    versions = _version_rows(connection)
    covered = _covered_versions(connection, set())
    groups: dict[str, list[sqlite3.Row]] = {}
    non_store: list[dict[str, str]] = []
    for row in versions:
        if _parse_object_key(row["object_key"]) is None:
            non_store.append(
                {"artifact_version_id": row["id"], "object_key": row["object_key"]}
            )
            continue
        groups.setdefault(row["object_key"], []).append(row)
    packed_keys = {
        key
        for key, group in groups.items()
        if not all(_version_covered(row, covered) for row in group)
    }
    entries: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        project_id, digest, generation_id = _parse_object_key(key) or ("", "", "")
        relative = object_relative(project_id, digest, generation_id)
        withheld = key not in packed_keys
        if withheld:
            declared = group[0]["content_hash"]
            size_bytes = int(group[0]["size_bytes"])
        else:
            source = resolve_contained(objects_root, relative)
            if not source.is_file():
                raise RecoveryError(FAILURE_MISSING_ORIGINAL, DETAIL_MISSING_ORIGINAL)
            data, file_digest = _read_hashed(source)
            mismatched = any(
                row["content_hash"] != file_digest or int(row["size_bytes"]) != len(data)
                for row in group
            )
            if mismatched:
                raise RecoveryError(FAILURE_CORRUPT, DETAIL_CORRUPT)
            _write_contained(originals_root, relative, data)
            declared = file_digest
            size_bytes = len(data)
        entries.append(
            {
                "object_key": key,
                "sha256": declared,
                "size_bytes": size_bytes,
                "relative_path": relative,
                "packed": not withheld,
                "withheld": "tombstone" if withheld else None,
                "artifact_version_ids": [row["id"] for row in group],
            }
        )
    for row in _stored_rows(connection):
        key = original_key(row["project_id"], row["sha256"], row["generation_id"])
        if any(entry["object_key"] == key for entry in entries):
            continue
        relative = object_relative(row["project_id"], row["sha256"], row["generation_id"])
        source = resolve_contained(objects_root, relative)
        if not source.is_file():
            raise RecoveryError(FAILURE_MISSING_ORIGINAL, DETAIL_MISSING_ORIGINAL)
        data, file_digest = _read_hashed(source)
        if file_digest != row["sha256"] or len(data) != int(row["byte_size"]):
            raise RecoveryError(FAILURE_CORRUPT, DETAIL_CORRUPT)
        _write_contained(originals_root, relative, data)
        entries.append(
            {
                "object_key": key,
                "sha256": file_digest,
                "size_bytes": len(data),
                "relative_path": relative,
                "packed": True,
                "withheld": None,
                "artifact_version_ids": [],
            }
        )
    return entries, non_store


def _place_originals(
    backup_dir: Path,
    staging: Path,
    manifest: dict[str, Any],
    live: _LiveDeletion | None,
) -> tuple[list[str], list[str]]:
    with _read_connection(backup_dir / _DATABASE_NAME) as connection:
        extra = _live_tombstone_pairs(live)
        withhold = _withhold_keys(connection, extra)
    originals_root = staging / "objects"
    originals_root.mkdir(parents=True, exist_ok=True)
    withheld: list[str] = []
    copied: list[str] = []
    for entry in manifest["originals"]:
        key = entry["object_key"]
        if entry["packed"] is not True:
            if key in withhold or entry.get("withheld") == "tombstone":
                withheld.append(key)
            continue
        if key in withhold:
            withheld.append(key)
            continue
        data, digest = _read_hashed(
            resolve_contained(backup_dir / _ORIGINALS_DIR, entry["relative_path"])
        )
        if digest != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise _Rejected(FAILURE_CORRUPT)
        _write_contained(originals_root, entry["relative_path"], data)
        copied.append(key)
    return withheld, copied


def _post_process(database: Path, live: _LiveDeletion | None) -> dict[str, Any]:
    engine = build_engine(f"sqlite:///{database.as_posix()}")
    try:
        try:
            with write_uow(engine) as session:
                revoked_sessions = _revoke_sessions(session, time.time())
                leases = _reset_leases(session)
                applied_rows = _apply_live(session, live) if live is not None else None
                inserted, removed = _rebuild_index(session)
            deletion_applied = live is not None
        except _ApplyFailed:
            with write_uow(engine) as session:
                revoked_sessions = _revoke_sessions(session, time.time())
                leases = _reset_leases(session)
                inserted, removed = _rebuild_index(session)
            applied_rows = None
            deletion_applied = False
    finally:
        engine.dispose()
    return {
        "sessions_revoked": revoked_sessions,
        "leases": leases,
        "applied_rows": applied_rows,
        "deletion_applied": deletion_applied,
        "index_rows_inserted": inserted,
        "index_rows_removed_for_tombstones": removed,
        "index_state": "rebuilt",
    }


def _rebuild_index(session: Any) -> tuple[int, int]:
    session.execute(text("DELETE FROM evidence_excerpt_index"))
    inserted = rebuild_evidence_index(session)
    removed = _prune_tombstoned_index(session)
    return inserted, removed


def _match_checks(
    snapshot: Path,
    restored: Path,
    live: _LiveDeletion | None,
    manifest: dict[str, Any],
    withheld: list[str],
) -> None:
    backup = _open_read(snapshot)
    dest = sqlite3.connect(restored)
    try:
        dest.execute("PRAGMA busy_timeout=5000")
        if _fetch(backup, _PAGE_SQL) != _fetch(dest, _PAGE_SQL):
            raise _CheckFailed("page_history")
        if _fetch(backup, _BRANCH_SQL) != _fetch(dest, _BRANCH_SQL):
            raise _CheckFailed("branches")
        if _fetch(backup, _DECISION_SQL) != _fetch(dest, _DECISION_SQL):
            raise _CheckFailed("decisions")
        if _fetch(backup, _ARTIFACT_SQL) != _fetch(dest, _ARTIFACT_SQL):
            raise _CheckFailed("artifact_hashes")
        if _fetch(dest, _GRANT_SQL) != _expected_status(
            _fetch(backup, _GRANT_SQL),
            {} if live is None else live.revoked_grants,
        ):
            raise _CheckFailed("permissions")
        if _fetch(dest, _MEMBERSHIP_SQL) != _expected_status(
            _fetch(backup, _MEMBERSHIP_SQL),
            {} if live is None else live.revoked_memberships,
            status_index=3,
            revoked_index=4,
        ):
            raise _CheckFailed("permissions")
        hashes = {row[4]: row[2] for row in _fetch(dest, _ARTIFACT_SQL)}
    finally:
        dest.close()
        backup.close()
    _match_files(restored.parent / "objects", manifest, withheld, hashes)


def _match_files(
    objects: Path,
    manifest: dict[str, Any],
    withheld: list[str],
    hashes: dict[str, str],
) -> None:
    withheld_set = set(withheld)
    for entry in manifest["originals"]:
        key = entry["object_key"]
        if key in hashes and hashes[key] != entry["sha256"]:
            raise _CheckFailed("artifact_hashes")
        path = objects.joinpath(*entry["relative_path"].split("/"))
        if key in withheld_set:
            if path.exists():
                raise _CheckFailed("artifact_hashes")
            continue
        if entry["packed"] is not True:
            continue
        if not path.is_file():
            raise _CheckFailed("artifact_hashes")
        data, digest = _read_hashed(path)
        if digest != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise _CheckFailed("artifact_hashes")


def _success_report(
    *,
    manifest: dict[str, Any],
    elapsed: float,
    opened: bool,
    live_state: str,
    post: dict[str, Any],
    withheld: list[str],
    copied: list[str],
) -> dict[str, Any]:
    index_state = post["index_state"]
    restored = ["relational_snapshot"]
    if copied:
        restored.append("originals")
    rebuilt = ["evidence_index"] if index_state == "rebuilt" else []
    not_restored = ["previews"]
    if index_state != "rebuilt":
        not_restored.append("evidence_index")
    report = {
        "succeeded": True,
        "failure": None,
        "opened": opened,
        "fully_automatic": opened and live_state == "applied",
        "deletion_list": live_state,
        "detail": None,
        "summary": _summary(opened=opened, index_state=index_state, withheld=bool(withheld)),
        "elapsed_seconds": elapsed,
        "backup_elapsed_seconds": manifest.get("elapsed_seconds"),
        "backed_up_at": manifest.get("backed_up_at"),
        "restored_at": time.time(),
        "schema_revision": SCHEMA_REVISION,
        "backup_method": manifest.get("backup_method"),
        "source_journal_mode": manifest.get("source_journal_mode"),
        "checks": {
            "page_history": "match",
            "branches": "match",
            "decisions": "match",
            "artifact_hashes": "match",
            "permissions": "match",
        },
        "restored": restored,
        "rebuilt": rebuilt,
        "not_restored": not_restored,
        "derivatives": {
            "evidence_index": {
                "state": index_state,
                "rows_inserted": post["index_rows_inserted"],
                "rows_removed_for_tombstones": post["index_rows_removed_for_tombstones"],
                "restored": False,
            },
            "previews": {"state": "pending_rebuild", "restored": False},
        },
        "originals_copied": copied,
        "originals_withheld": withheld,
        "sessions_revoked": post["sessions_revoked"],
        "leases_reset": post["leases"],
        "tombstones_applied": 0 if post["applied_rows"] is None else post["applied_rows"][0],
        "revocations_applied": 0 if post["applied_rows"] is None else post["applied_rows"][1],
        "loss_window": (
            "快照只包含备份调用完成时已经提交的库，以及当时打包的原件。"
            "之后才提交的普通写入不在这份快照里。"
            "恢复时如果读到了当前墓碑和撤权，会补到隔离副本上，不写回源库。"
            "这里的耗时是这一次本地测量，不能外推成生产承诺。"
        ),
        "environment": _environment(),
        "lease_note": "运行中的任务已去掉旧 owner，状态回到 queued，等幂等重试。没有新的任务状态。",
    }
    if not opened:
        report["limitation"] = "当前删除清单不可用或没有套上，项目保持离线。"
    return report


def _summary(*, opened: bool, index_state: str, withheld: bool) -> str:
    if not opened:
        return (
            "隔离目录里有一份未开放的副本。当前删除清单不可用，"
            "不能把这次当成全自动恢复成功。预览尚未重建，不能视为已恢复。"
        )
    if withheld:
        originals = "已恢复业务。已删除的原件没有放回隔离目录。"
    else:
        originals = "已恢复业务与原件。"
    if index_state == "rebuilt":
        index = "搜索索引已重建。"
    else:
        index = "搜索索引尚未重建，不能视为已恢复。"
    return originals + index + "预览尚未重建，不能视为已恢复。"


def _failure(failure: str, started: float) -> RestoreResult:
    elapsed = time.perf_counter() - started
    report = {
        "succeeded": False,
        "failure": failure,
        "opened": False,
        "fully_automatic": False,
        "detail": _DETAILS[failure],
        "summary": "没有恢复成功。",
        "elapsed_seconds": elapsed,
        "restored": [],
        "rebuilt": [],
        "not_restored": ["previews", "evidence_index", "originals"],
        "derivatives": {
            "evidence_index": {"state": "not_rebuilt", "restored": False},
            "previews": {"state": "pending_rebuild", "restored": False},
        },
        "environment": _environment(),
    }
    return RestoreResult(False, failure, False, elapsed, report)


def _incomplete(started: float, detail: str) -> RestoreResult:
    elapsed = time.perf_counter() - started
    report = {
        "succeeded": False,
        "failure": None,
        "opened": False,
        "fully_automatic": False,
        "detail": detail,
        "summary": "没有恢复成功。",
        "elapsed_seconds": elapsed,
        "restored": [],
        "rebuilt": [],
        "not_restored": ["previews", "evidence_index"],
        "environment": _environment(),
    }
    return RestoreResult(False, None, False, elapsed, report)


def _classify_manifest(manifest: object) -> str | None:
    if not isinstance(manifest, dict):
        return FAILURE_CORRUPT
    format_version = manifest.get("format_version")
    if type(format_version) is not int or format_version != FORMAT_VERSION:
        return FAILURE_INCOMPATIBLE_VERSION
    if manifest.get("schema_revision") != SCHEMA_REVISION:
        return FAILURE_INCOMPATIBLE_VERSION
    if not _hex64(manifest.get("database_sha256")):
        return FAILURE_CORRUPT
    if type(manifest.get("database_bytes")) is not int:
        return FAILURE_CORRUPT
    if not isinstance(manifest.get("originals"), list):
        return FAILURE_CORRUPT
    return None


def _package_problem(backup_dir: Path, manifest: dict[str, Any]) -> None:
    database = backup_dir / _DATABASE_NAME
    if not database.is_file():
        raise _Rejected(FAILURE_CORRUPT)
    size, digest = _file_sha256(database)
    if digest != manifest["database_sha256"] or size != manifest["database_bytes"]:
        raise _Rejected(FAILURE_CORRUPT)
    try:
        if not _lone_ok(database):
            raise _Rejected(FAILURE_CORRUPT)
    except sqlite3.DatabaseError as error:
        raise _Rejected(FAILURE_CORRUPT) from error
    with _read_connection(database) as connection:
        if _schema_revision(connection) != SCHEMA_REVISION:
            raise _Rejected(FAILURE_INCOMPATIBLE_VERSION)
        if not _integrity_connection(connection):
            raise _Rejected(FAILURE_CORRUPT)
        revisions, epochs = _revision_catalog(connection)
        same_revisions = revisions == manifest.get("project_revisions")
        same_epochs = epochs == manifest.get("tombstone_epoch")
        if not same_revisions or not same_epochs:
            raise _Rejected(FAILURE_CORRUPT)
        version_hashes = {
            row["object_key"]: row["content_hash"] for row in _version_rows(connection)
        }
    for entry in manifest["originals"]:
        if not _valid_entry(entry):
            raise _Rejected(FAILURE_CORRUPT)
        recorded = version_hashes.get(entry["object_key"])
        if recorded is not None and recorded != entry["sha256"]:
            raise _Rejected(FAILURE_CORRUPT)
        if entry["packed"] is not True:
            continue
        try:
            path = resolve_contained(backup_dir / _ORIGINALS_DIR, entry["relative_path"])
        except PathEscape as error:
            raise _Rejected(FAILURE_CORRUPT) from error
        if not path.is_file():
            raise _Rejected(FAILURE_MISSING_ORIGINAL)
        data, file_digest = _read_hashed(path)
        if file_digest != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise _Rejected(FAILURE_CORRUPT)


def _verify_packed(staging: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["originals"]:
        if entry["packed"] is not True:
            continue
        path = resolve_contained(staging / _ORIGINALS_DIR, entry["relative_path"])
        data, digest = _read_hashed(path)
        if digest != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise RecoveryError(FAILURE_CORRUPT, DETAIL_CORRUPT)


def _read_live(path: Path | None) -> tuple[_LiveDeletion | None, str]:
    if path is None or not path.is_file():
        return None, "unavailable"
    try:
        connection = _open_read(path)
    except (sqlite3.Error, OSError):
        return None, "unavailable"
    connection.row_factory = sqlite3.Row
    try:
        tombstones = tuple(
            connection.execute(
                """
                SELECT id, project_id, object_type, object_id, reason_category,
                       access_epoch, audit_event_id, created_at
                FROM privacy_tombstones
                """
            ).fetchall()
        )
        audits = {
            row["id"]: row
            for row in connection.execute(
                """
                SELECT id, project_id, actor_id, command, subject_type, subject_id,
                       project_revision, payload, created_at
                FROM audit_events
                WHERE id IN (SELECT audit_event_id FROM privacy_tombstones)
                """
            ).fetchall()
        }
        grants = {
            row["id"]: float(row["revoked_at"])
            for row in connection.execute(
                "SELECT id, revoked_at FROM object_grants WHERE status = 'revoked'"
            ).fetchall()
        }
        memberships = {
            row["id"]: float(row["revoked_at"])
            for row in connection.execute(
                "SELECT id, revoked_at FROM memberships WHERE status = 'revoked'"
            ).fetchall()
        }
        epochs = {
            row["id"]: int(row["access_epoch"])
            for row in connection.execute("SELECT id, access_epoch FROM projects").fetchall()
        }
    except sqlite3.Error:
        return None, "unavailable"
    finally:
        connection.close()
    return (
        _LiveDeletion(tombstones, audits, grants, memberships, epochs),
        "applied",
    )


class _ApplyFailed(Exception):
    pass


class _CheckFailed(Exception):
    def __init__(self, check: str) -> None:
        self.check = check
        super().__init__(check)


def _apply_live(session: Any, live: _LiveDeletion | None) -> tuple[int, int] | None:
    if live is None:
        return None
    tombstones = 0
    for row in live.tombstones:
        found = session.execute(
            text(
                """
                SELECT id FROM privacy_tombstones
                WHERE project_id = :project_id AND object_type = :object_type
                  AND object_id = :object_id
                """
            ),
            {
                "project_id": row["project_id"],
                "object_type": row["object_type"],
                "object_id": row["object_id"],
            },
        ).first()
        if found is not None:
            continue
        audit = live.audits.get(row["audit_event_id"])
        if audit is None:
            raise _ApplyFailed()
        actor = session.execute(
            text("SELECT id FROM accounts WHERE id = :id"),
            {"id": audit["actor_id"]},
        ).first()
        project = session.execute(
            text("SELECT id FROM projects WHERE id = :id"),
            {"id": row["project_id"]},
        ).first()
        if actor is None or project is None:
            raise _ApplyFailed()
        existing_audit = session.execute(
            text("SELECT id FROM audit_events WHERE id = :id"),
            {"id": audit["id"]},
        ).first()
        if existing_audit is None:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                      id, project_id, actor_id, command, subject_type, subject_id,
                      project_revision, payload, created_at
                    ) VALUES (
                      :id, :project_id, :actor_id, :command, :subject_type, :subject_id,
                      :project_revision, :payload, :created_at
                    )
                    """
                ),
                dict(audit),
            )
        session.execute(
            text(
                """
                INSERT INTO privacy_tombstones (
                  id, project_id, object_type, object_id, reason_category,
                  access_epoch, audit_event_id, created_at
                ) VALUES (
                  :id, :project_id, :object_type, :object_id, :reason_category,
                  :access_epoch, :audit_event_id, :created_at
                )
                """
            ),
            dict(row),
        )
        tombstones += 1
    revocations = 0
    for grant_id, revoked_at in live.revoked_grants.items():
        current = session.execute(
            text("SELECT status FROM object_grants WHERE id = :id"),
            {"id": grant_id},
        ).scalar()
        if current != "active":
            continue
        session.execute(
            text(
                """
                UPDATE object_grants
                SET status = 'revoked', revoked_at = :revoked_at
                WHERE id = :id AND status = 'active'
                """
            ),
            {"id": grant_id, "revoked_at": revoked_at},
        )
        revocations += 1
    for membership_id, revoked_at in live.revoked_memberships.items():
        current = session.execute(
            text("SELECT status FROM memberships WHERE id = :id"),
            {"id": membership_id},
        ).scalar()
        if current != "active":
            continue
        session.execute(
            text(
                """
                UPDATE memberships
                SET status = 'revoked', revoked_at = :revoked_at
                WHERE id = :id AND status = 'active'
                """
            ),
            {"id": membership_id, "revoked_at": revoked_at},
        )
        revocations += 1
    for project_id, epoch in live.access_epochs.items():
        session.execute(
            text(
                """
                UPDATE projects
                SET access_epoch = :epoch
                WHERE id = :id AND access_epoch < :epoch
                """
            ),
            {"id": project_id, "epoch": epoch},
        )
    return tombstones, revocations


def _revoke_sessions(session: Any, now: float) -> int:
    pending = session.execute(
        text("SELECT count(*) FROM sessions WHERE revoked_at IS NULL")
    ).scalar_one()
    session.execute(
        text(
            """
            UPDATE sessions
            SET revoked_at = CASE WHEN :now >= created_at THEN :now ELSE created_at END
            WHERE revoked_at IS NULL
            """
        ),
        {"now": now},
    )
    return int(pending)


def _reset_leases(session: Any) -> dict[str, int | str]:
    jobs = int(
        session.execute(text("SELECT count(*) FROM jobs WHERE status = 'running'")).scalar_one()
    )
    tasks = int(
        session.execute(
            text("SELECT count(*) FROM run_tasks WHERE status = 'running'")
        ).scalar_one()
    )
    deliveries = int(
        session.execute(
            text("SELECT count(*) FROM outbox_deliveries WHERE state = 'leased'")
        ).scalar_one()
    )
    session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'queued', lease_owner = NULL, lease_until = NULL
            WHERE status = 'running'
            """
        )
    )
    session.execute(
        text(
            """
            UPDATE run_tasks
            SET status = 'queued', lease_owner = NULL, lease_until = NULL
            WHERE status = 'running'
            """
        )
    )
    session.execute(
        text(
            """
            UPDATE outbox_deliveries
            SET state = 'pending', lease_owner = NULL, lease_until = NULL
            WHERE state = 'leased'
            """
        )
    )
    return {
        "jobs": jobs,
        "run_tasks": tasks,
        "outbox_deliveries": deliveries,
        "pending_state": "queued",
    }


def _prune_tombstoned_index(session: Any) -> int:
    result = session.execute(
        text(
            """
            DELETE FROM evidence_excerpt_index
            WHERE anchor_id IN (
              SELECT object_id FROM privacy_tombstones WHERE object_type = 'evidence_anchor'
            )
            OR artifact_version_id IN (
              SELECT object_id FROM privacy_tombstones WHERE object_type = 'artifact_version'
            )
            OR artifact_version_id IN (
              SELECT id FROM artifact_versions
              WHERE artifact_id IN (
                SELECT object_id FROM privacy_tombstones WHERE object_type = 'artifact'
              )
            )
            """
        )
    )
    return int(result.rowcount or 0)


def _withhold_keys(connection: sqlite3.Connection, extra: set[tuple[str, str]]) -> set[str]:
    covered = _covered_versions(connection, extra)
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in _version_rows(connection):
        if _parse_object_key(row["object_key"]) is None:
            continue
        groups.setdefault(row["object_key"], []).append(row)
    return {
        key
        for key, group in groups.items()
        if group and all(_version_covered(row, covered) for row in group)
    }


def _covered_versions(
    connection: sqlite3.Connection,
    extra: set[tuple[str, str]],
) -> tuple[set[str], set[str]]:
    versions = {
        row["object_id"]
        for row in connection.execute(
            "SELECT object_id FROM privacy_tombstones WHERE object_type = 'artifact_version'"
        ).fetchall()
    }
    artifacts = {
        row["object_id"]
        for row in connection.execute(
            "SELECT object_id FROM privacy_tombstones WHERE object_type = 'artifact'"
        ).fetchall()
    }
    for object_type, object_id in extra:
        if object_type == "artifact_version":
            versions.add(object_id)
        elif object_type == "artifact":
            artifacts.add(object_id)
    return versions, artifacts


def _version_covered(row: sqlite3.Row, covered: tuple[set[str], set[str]]) -> bool:
    versions, artifacts = covered
    return row["id"] in versions or row["artifact_id"] in artifacts


def _live_tombstone_pairs(live: _LiveDeletion | None) -> set[tuple[str, str]]:
    if live is None:
        return set()
    return {(row["object_type"], row["object_id"]) for row in live.tombstones}


def _version_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(connection.execute(_ARTIFACT_SQL).fetchall())


def _stored_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(
        connection.execute(
            """
            SELECT project_id, sha256, generation_id, byte_size
            FROM stored_objects
            WHERE state IN ('ready', 'reserved')
            """
        ).fetchall()
    )


def _revision_catalog(
    connection: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projects = connection.execute(
        "SELECT id, revision, access_epoch FROM projects ORDER BY id"
    ).fetchall()
    tombstones = {
        row[0]: (int(row[1] or 0), int(row[2]))
        for row in connection.execute(
            """
            SELECT project_id, MAX(access_epoch), COUNT(*)
            FROM privacy_tombstones
            GROUP BY project_id
            """
        ).fetchall()
    }
    revisions = [{"project_id": row[0], "revision": int(row[1])} for row in projects]
    epochs = []
    for row in projects:
        epoch, count = tombstones.get(row[0], (0, 0))
        epochs.append(
            {
                "project_id": row[0],
                "project_revision": int(row[1]),
                "access_epoch": int(row[2]),
                "tombstone_epoch": epoch,
                "tombstone_count": count,
            }
        )
    return revisions, epochs


def _schema_revision(connection: sqlite3.Connection) -> str:
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise _Rejected(FAILURE_INCOMPATIBLE_VERSION)
    return str(rows[0][0])


def _expected_status(
    rows: list[tuple[Any, ...]],
    revoked: dict[str, float],
    *,
    status_index: int = 5,
    revoked_index: int = 6,
) -> list[tuple[Any, ...]]:
    expected: list[tuple[Any, ...]] = []
    for row in rows:
        if row[0] in revoked and row[status_index] == "active":
            updated = list(row)
            updated[status_index] = "revoked"
            updated[revoked_index] = revoked[row[0]]
            expected.append(tuple(updated))
        else:
            expected.append(tuple(row))
    return expected


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    key = entry.get("object_key")
    relative = entry.get("relative_path")
    if not isinstance(key, str) or not isinstance(relative, str):
        return False
    if not _hex64(entry.get("sha256")) or type(entry.get("size_bytes")) is not int:
        return False
    if entry.get("packed") is True:
        return entry.get("withheld") is None
    if entry.get("packed") is False:
        return entry.get("withheld") == "tombstone"
    return False


def _hex64(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _parse_object_key(object_key: str) -> tuple[str, str, str] | None:
    if not isinstance(object_key, str) or "\\" in object_key or object_key.startswith("/"):
        return None
    parts = object_key.split("/")
    if len(parts) != 6:
        return None
    root, project_id, kind, digest, generation_id, leaf = parts
    if (root, kind, leaf) != ("objects", "sha256", "original"):
        return None
    if not _hex64(digest):
        return None
    if not is_storage_segment(project_id) or not is_storage_segment(generation_id):
        return None
    return project_id, digest, generation_id


def _fetch(connection: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return list(connection.execute(sql).fetchall())


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        return _integrity_connection(connection)
    finally:
        connection.close()


def _integrity_connection(connection: sqlite3.Connection) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    return integrity == [("ok",)] and foreign == []


def _lone_ok(path: Path) -> bool:
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        lone = Path(tmp) / _DATABASE_NAME
        shutil.copyfile(path, lone)
        connection = sqlite3.connect(lone)
        try:
            if not _integrity_connection(connection):
                return False
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()
    return len(rows) == 1


def _journal_mode(path: Path) -> str:
    with _read_connection(path) as connection:
        row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]) if row is not None else ""


def _open_read(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_uri(path, "ro"), uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextmanager
def _read_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = _open_read(path)
    try:
        yield connection
    finally:
        connection.close()


def _open_query_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _uri(path: Path, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _read_hashed(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _write_contained(root: Path, relative: str, data: bytes) -> None:
    path = resolve_contained(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_manifest(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
    }


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise ValueError(f"{label} is not a file")
    return path


def _require_dir(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory")
    return path


def _require_absent(path: Path, label: str) -> None:
    if path.exists():
        raise ValueError(f"{label} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)


def _reject_destination(
    backup_dir: Path,
    destination: Path,
    live_database: Path | None,
) -> None:
    if destination.exists():
        raise ValueError("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    dest = destination.resolve()
    if dest == backup_dir.resolve():
        raise ValueError("destination must be separate from the backup")
    if live_database is not None and live_database.is_file():
        if dest == live_database.resolve().parent:
            raise ValueError("destination must be an isolated directory")
        if (destination / _RESTORED_NAME).resolve() == live_database.resolve():
            raise ValueError("refusing to write the source database")
