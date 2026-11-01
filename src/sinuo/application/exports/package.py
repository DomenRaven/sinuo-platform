"""Freeze one export snapshot, then recheck rights before generating or sending it.

The job row is the export. ``job_kind`` is ``export``. The payload written at
registration is the input snapshot: source ids and the permission labels from
that moment. Claim, file generation, and download do not treat those labels as
permission. Each one calls the authorizer again. The report is a fixed template.
This module does not call a model and does not write a design version.
"""

import hashlib
import io
import json
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.assets.download import OriginalUnreadable, download_original
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import (
    ActionForbidden,
    AuthRequired,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import canonical_audit_payload
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    EvidenceAnchor,
    Job,
    Page,
    PageVersion,
    PrivacyTombstone,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent
from sinuo.workers.jobs import claim_job, finalize_job, finish_job_attempt

_OWNER = "export-worker"
_TTL_SECONDS = 60.0
_PNG = b"\x89PNG\r\n\x1a\n"
_FRAGMENT_LIMIT = 2000
_FORMAT = "markdown"

@dataclass(frozen=True)
class ExportRun:
    export_id: str
    status: str
    output_ref: str | None


@dataclass(frozen=True)
class _Prepared:
    owner: str
    fencing_token: int
    input_hash: str
    files: dict[str, bytes]
    manifest_hash: str


def open_export(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    source_manifest: dict[str, list[str]],
    include_originals: bool,
    export_format: str,
    request_hash: str,
    now: float,
) -> dict[str, Any]:
    """Insert one queued export job and its frozen snapshot. Does not render a file."""

    if export_format != _FORMAT or type(include_originals) is not bool:
        raise NotFoundOrHidden()
    authorizer = SessionAuthorizer(session)
    gate = _probe(
        authorizer,
        Action.REPORT_EXPORT,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    if gate != "allow":
        _raise_gate(gate, Action.REPORT_EXPORT)
    epoch = session.scalar(select(Project.access_epoch).where(Project.id == project_id))
    if type(epoch) is not int:
        raise NotFoundOrHidden()
    export_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    audit_event_id = str(uuid.uuid4())
    pages = list(source_manifest["page_version_ids"])
    artifacts = list(source_manifest["artifact_version_ids"])
    evidence = list(source_manifest["evidence_anchor_ids"])
    frozen_manifest = {
        "page_version_ids": pages,
        "artifact_version_ids": artifacts,
        "evidence_anchor_ids": evidence,
    }
    scope = {
        "access_epoch": epoch,
        "report_export": "allow",
        "page_read": {
            item: _page_probe(authorizer, principal, project_id, item) for item in pages
        },
        "original_read": {
            item: _artifact_probe(
                authorizer, principal, project_id, item, Action.ARTIFACT_ORIGINAL_READ
            )
            for item in artifacts
        },
        "preview_read": {
            item: _artifact_probe(authorizer, principal, project_id, item, Action.PREVIEW_READ)
            for item in artifacts
        },
        "evidence_read": {
            item: _evidence_probe(authorizer, principal, project_id, item) for item in evidence
        },
    }
    snapshot = {
        "schema_version": 1,
        "export_job_id": export_id,
        "input_snapshot_id": snapshot_id,
        "actor_id": principal.account_id,
        "project_id": project_id,
        "format": _FORMAT,
        "include_originals": include_originals,
        "access_epoch": epoch,
        "source_manifest": frozen_manifest,
        "permission_scope": scope,
        "audit_event_id": audit_event_id,
        "latest": False,
    }
    payload = _dump(snapshot).decode("utf-8")
    job = Job(
            id=export_id,
            project_id=project_id,
            job_kind="export",
            status="queued",
            available_at=0.0,
            attempt_no=0,
            lease_owner=None,
            lease_until=None,
            fencing_token=0,
            input_hash=request_hash,
            output_ref=None,
            last_error=None,
            subject_type="export_snapshot",
            subject_id=snapshot_id,
            source_event_id=None,
            payload=payload,
            created_at=now,
        )
    session.add(job)
    audit_body = {
        "export_job_id": export_id,
        "input_snapshot_id": snapshot_id,
        "page_count": len(pages),
        "artifact_count": len(artifacts),
        "evidence_count": len(evidence),
    }
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.REPORT_EXPORT.value,
            subject_type="export",
            subject_id=export_id,
            project_revision=None,
            payload=canonical_audit_payload(audit_body),
            created_at=now,
        )
    )
    session.add(
        OutboxEvent(
            event_id=str(uuid.uuid4()),
            project_id=project_id,
            event_type="export.requested",
            schema_version=1,
            payload=audit_body,
            created_at=now,
        )
    )
    session.flush()
    return export_view(job, command=True)


def require_export(session: Session, export_id: str) -> Job:
    job = session.get(Job, export_id)
    if job is None or job.job_kind != "export":
        raise NotFoundOrHidden()
    return job


def export_view(job: Job, *, command: bool) -> dict[str, Any]:
    """Public fields. ``latest`` stays false so a stored snapshot is not the live head."""

    snapshot = _require_snapshot(job.payload)
    body: dict[str, Any] = {
        "export_job_id": job.id,
        "input_snapshot_id": snapshot["input_snapshot_id"],
        "status": job.status,
        "latest": False,
        "format": snapshot["format"],
        "include_originals": snapshot["include_originals"],
        "access_epoch_at_input": snapshot["access_epoch"],
        "source_manifest": {
            "page_version_ids": list(snapshot["source_manifest"]["page_version_ids"]),
            "artifact_version_ids": list(snapshot["source_manifest"]["artifact_version_ids"]),
            "evidence_anchor_ids": list(snapshot["source_manifest"]["evidence_anchor_ids"]),
        },
    }
    if command:
        body["audit_event_id"] = snapshot["audit_event_id"]
    return body


def compose_export(
    session: Session,
    *,
    principal: Principal,
    snapshot: dict[str, Any],
    objects_root: Path | None,
    derived_root: Path | None,
) -> dict[str, bytes]:
    """Build the package from current rights. The frozen scope is not consulted."""

    project_id = snapshot["project_id"]
    include_originals = snapshot["include_originals"]
    manifest_body = snapshot["source_manifest"]
    authorizer = SessionAuthorizer(session)
    gate = _probe(
        authorizer,
        Action.REPORT_EXPORT,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    if gate != "allow":
        _raise_gate(gate, Action.REPORT_EXPORT)
    files: dict[str, bytes] = {}
    items: list[dict[str, Any]] = []
    lines = [
        "# 报告草稿",
        "",
        f"输入快照：{snapshot['input_snapshot_id']}",
        f"输入授权纪元：{snapshot['access_epoch']}",
        "这份草稿对应导出当时冻结的来源，不是最新来源。",
        "最新来源：否",
        "条目按当前权限重校。",
        "",
        "## 页版本",
    ]
    for page_version_id in _sorted_ids(manifest_body["page_version_ids"]):
        gate = _page_probe(authorizer, principal, project_id, page_version_id)
        if gate == "allow":
            titled = _page_line(session, project_id, page_version_id)
            lines.append(f"- 页版本 {page_version_id}：已纳入，{titled}")
            items.append({"id": page_version_id, "kind": "page_version", "status": "included"})
        else:
            lines.append(f"- 页版本 {page_version_id}：{_phrase(gate)}")
            items.append(
                {"id": page_version_id, "kind": "page_version", "status": _item_status(gate)}
            )
    lines.extend(["", "## 作品"])
    for version_id in _sorted_ids(manifest_body["artifact_version_ids"]):
        original_gate = _artifact_probe(
            authorizer,
            principal,
            project_id,
            version_id,
            Action.ARTIFACT_ORIGINAL_READ,
        )
        preview_gate = _artifact_probe(
            authorizer,
            principal,
            project_id,
            version_id,
            Action.PREVIEW_READ,
        )
        original_status, original_bytes = _original_bytes(
            session,
            principal,
            authorizer,
            project_id=project_id,
            version_id=version_id,
            gate=original_gate,
            include_originals=include_originals,
            objects_root=objects_root,
        )
        thumb_status, thumb_bytes = _thumbnail_bytes(
            derived_root,
            version_id,
            preview_gate,
        )
        title = ""
        if original_status == "included" or thumb_status == "included":
            title = _artifact_title(session, project_id, version_id)
        original_title = title if original_status == "included" else ""
        thumb_title = title if thumb_status == "included" else ""
        lines.append(_artifact_line(version_id, "原件", original_status, original_title))
        lines.append(_artifact_line(version_id, "缩略图", thumb_status, thumb_title))
        original_item: dict[str, Any] = {"status": original_status}
        thumb_item: dict[str, Any] = {"status": thumb_status}
        if original_bytes is not None:
            path = f"originals/{version_id}"
            files[path] = original_bytes
            original_item["sha256"] = _sha256(original_bytes)
        if thumb_bytes is not None:
            path = f"thumbnails/{version_id}.png"
            files[path] = thumb_bytes
            thumb_item["sha256"] = _sha256(thumb_bytes)
        items.append(
            {
                "id": version_id,
                "kind": "artifact_version",
                "original": original_item,
                "thumbnail": thumb_item,
            }
        )
    lines.extend(["", "## 来源片段"])
    for anchor_id in _sorted_ids(manifest_body["evidence_anchor_ids"]):
        status, fragment = _excerpt(session, authorizer, principal, project_id, anchor_id)
        if fragment is None:
            lines.append(f"- 锚点 {anchor_id}：来源片段{_phrase(status)}")
        else:
            lines.append(f"- 锚点 {anchor_id}：来源片段已纳入 {fragment}")
        items.append(
            {
                "excerpt": {"status": status},
                "id": anchor_id,
                "kind": "evidence_anchor",
            }
        )
    report = ("\n".join(lines) + "\n").encode("utf-8")
    files["report.md"] = report
    listed = [
        {"byte_size": len(payload), "path": name, "sha256": _sha256(payload)}
        for name, payload in sorted(files.items())
    ]
    manifest = {
        "access_epoch_at_input": snapshot["access_epoch"],
        "export_job_id": snapshot["export_job_id"],
        "files": listed,
        "format": _FORMAT,
        "include_originals": include_originals,
        "input_snapshot_id": snapshot["input_snapshot_id"],
        "items": items,
        "latest": False,
        "schema_version": 1,
    }
    files["manifest.json"] = _dump(manifest)
    return files


def zip_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(filename=name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, files[name])
    return buffer.getvalue()


def compose_job(
    session: Session,
    job: Job,
    principal: Principal,
    *,
    objects_root: Path | None,
    derived_root: Path | None,
) -> dict[str, bytes]:
    """Recheck the caller, then build a package for this job's frozen snapshot."""

    snapshot = _require_snapshot(job.payload)
    if snapshot["project_id"] != job.project_id or snapshot["export_job_id"] != job.id:
        raise NotFoundOrHidden()
    return compose_export(
        session,
        principal=principal,
        snapshot=snapshot,
        objects_root=objects_root,
        derived_root=derived_root,
    )


def run_export_job(
    engine,
    export_id: str,
    *,
    exports_root: Path,
    objects_root: Path | None,
    derived_root: Path | None,
) -> ExportRun:
    """Claim one export, recheck ``report.export``, and write only the current package."""

    if not isinstance(exports_root, Path) or not exports_root.is_dir():
        raise ValueError("exports_root must be a directory")
    prepared: _Prepared | None = None
    with write_uow(engine) as session:
        early = _idle(session, export_id)
        if early is not None:
            return early
        prepared = _claim_and_compose(
            session,
            export_id,
            objects_root=objects_root,
            derived_root=derived_root,
        )
        if isinstance(prepared, ExportRun):
            return prepared
    assert prepared is not None
    _write_tree(exports_root, export_id, prepared.manifest_hash, prepared.files)
    with write_uow(engine) as session:
        finalize_job(
            session,
            job_id=export_id,
            owner=prepared.owner,
            fencing_token=prepared.fencing_token,
            input_hash=prepared.input_hash,
            output_ref=prepared.manifest_hash,
            consumer_name="export",
            effect_kind="export.package",
        )
    return ExportRun(export_id, "succeeded", prepared.manifest_hash)


def _idle(session: Session, export_id: str) -> ExportRun | None:
    job = require_export(session, export_id)
    if job.status in {"queued", "running"}:
        return None
    return ExportRun(job.id, job.status, job.output_ref)


def _claim_and_compose(
    session: Session,
    export_id: str,
    *,
    objects_root: Path | None,
    derived_root: Path | None,
) -> _Prepared | ExportRun:
    job = require_export(session, export_id)
    input_hash = job.input_hash
    snapshot = _require_snapshot(job.payload)
    if snapshot["export_job_id"] != job.id or snapshot["project_id"] != job.project_id:
        raise NotFoundOrHidden()
    attempt = claim_job(
        session,
        job_id=job.id,
        owner=_OWNER,
        input_hash=input_hash,
        ttl_seconds=_TTL_SECONDS,
    )
    try:
        principal = _actor(session, snapshot["actor_id"])
    except AuthRequired:
        _fail_unauthorized(session, job.id)
        return ExportRun(job.id, "failed", None)
    authorizer = SessionAuthorizer(session)
    gate = _probe(
        authorizer,
        Action.REPORT_EXPORT,
        ResourceRef(project_id=job.project_id, scope_type="project"),
        principal,
    )
    if gate != "allow":
        _fail_unauthorized(session, job.id)
        return ExportRun(job.id, "failed", None)
    files = compose_export(
        session,
        principal=principal,
        snapshot=snapshot,
        objects_root=objects_root,
        derived_root=derived_root,
    )
    manifest_hash = hashlib.sha256(files["manifest.json"]).hexdigest()
    return _Prepared(_OWNER, attempt.fencing_token, attempt.input_hash, files, manifest_hash)


def _fail_unauthorized(session: Session, export_id: str) -> None:
    moment = time.time()
    finish_job_attempt(
        session,
        job_id=export_id,
        category="unauthorized",
        max_attempts=1,
        deadline=moment + _TTL_SECONDS,
        available_at=moment + _TTL_SECONDS,
    )


def _actor(session: Session, account_id: object) -> Principal:
    if not isinstance(account_id, str):
        raise AuthRequired()
    account = session.get(Account, account_id)
    if account is None or account.status != "active":
        raise AuthRequired()
    return Principal(account.id, _OWNER, account.auth_epoch)


def _page_probe(authorizer, principal, project_id: str, page_version_id: str) -> str:
    return _probe(
        authorizer,
        Action.PAGE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="page_version",
            page_version_id=page_version_id,
        ),
        principal,
    )


def _artifact_probe(authorizer, principal, project_id: str, version_id: str, action: Action) -> str:
    return _probe(
        authorizer,
        action,
        ResourceRef(
            project_id=project_id,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        ),
        principal,
    )


def _evidence_probe(authorizer, principal, project_id: str, anchor_id: str) -> str:
    return _probe(
        authorizer,
        Action.EVIDENCE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="evidence_anchor",
            evidence_anchor_id=anchor_id,
        ),
        principal,
    )


def _probe(authorizer, action: Action, resource: ResourceRef, principal: Principal) -> str:
    try:
        authorizer.authorize(action, resource, principal)
    except SourceRedacted:
        return "tombstoned"
    except ActionForbidden:
        return "forbidden"
    except NotFoundOrHidden:
        return "hidden"
    return "allow"


def _raise_gate(gate: str, action: Action) -> None:
    if gate == "tombstoned":
        raise SourceRedacted()
    if gate == "hidden":
        raise NotFoundOrHidden()
    raise ActionForbidden(action.value)


def _original_bytes(
    session: Session,
    principal: Principal,
    authorizer: SessionAuthorizer,
    *,
    project_id: str,
    version_id: str,
    gate: str,
    include_originals: bool,
    objects_root: Path | None,
) -> tuple[str, bytes | None]:
    if not include_originals:
        if gate == "tombstoned":
            return "tombstoned", None
        return "not_requested", None
    if gate == "tombstoned":
        return "tombstoned", None
    if gate != "allow":
        return _item_status(gate), None
    if not isinstance(objects_root, Path) or not objects_root.is_dir():
        return "missing", None
    try:
        downloaded = download_original(
            session,
            principal,
            authorizer,
            project_id=project_id,
            artifact_version_id=version_id,
            objects_root=objects_root,
        )
    except SourceRedacted:
        return "tombstoned", None
    except ActionForbidden:
        return "omitted", None
    except (OriginalUnreadable, NotFoundOrHidden, OSError):
        return "missing", None
    return "included", downloaded.body


def _thumbnail_bytes(
    derived_root: Path | None,
    version_id: str,
    gate: str,
) -> tuple[str, bytes | None]:
    if gate == "tombstoned":
        return "tombstoned", None
    if gate != "allow":
        return _item_status(gate), None
    payload = _read_thumbnail(derived_root, version_id)
    if payload is None:
        return "missing", None
    return "included", payload


def _read_thumbnail(derived_root: Path | None, version_id: str) -> bytes | None:
    if not isinstance(derived_root, Path) or not derived_root.is_dir():
        return None
    if not is_storage_segment(version_id):
        return None
    relative = f"previews/{version_id}.png"
    try:
        path = resolve_contained(derived_root, relative)
    except PathEscape:
        return None
    if not path.is_file():
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload.startswith(_PNG):
        return None
    return payload


def _excerpt(
    session,
    authorizer,
    principal,
    project_id: str,
    anchor_id: str,
) -> tuple[str, str | None]:
    gate = _evidence_probe(authorizer, principal, project_id, anchor_id)
    if gate == "tombstoned":
        return "tombstoned", None
    if gate != "allow":
        return _item_status(gate), None
    anchor = session.get(EvidenceAnchor, anchor_id)
    if anchor is None or anchor.project_id != project_id:
        return "missing", None
    if _tombstoned(session, project_id, "artifact_version", anchor.artifact_version_id):
        return "tombstoned", None
    fragment = _matching_fragment(anchor.locator_json, anchor.excerpt_hash)
    if fragment is None:
        return "missing", None
    return "included", fragment


def _matching_fragment(raw: str, excerpt_hash: str) -> str | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    for key in ("excerpt", "marker"):
        value = parsed.get(key)
        if not isinstance(value, str) or len(value) > _FRAGMENT_LIMIT:
            continue
        if hashlib.sha256(value.encode("utf-8")).hexdigest() == excerpt_hash:
            return value
    return None


def _page_line(session: Session, project_id: str, page_version_id: str) -> str:
    version = session.get(PageVersion, page_version_id)
    if version is None or version.project_id != project_id:
        return "标题未记载"
    page = session.get(Page, version.page_id)
    if page is None or page.project_id != project_id:
        return f"版本号 {version.version_no}"
    title = " ".join(page.title.split())
    return f"版本号 {version.version_no}，标题 {title}"


def _artifact_title(session: Session, project_id: str, version_id: str) -> str:
    version = session.get(ArtifactVersion, version_id)
    if version is None or version.project_id != project_id:
        return ""
    artifact = session.get(Artifact, version.artifact_id)
    if artifact is None or artifact.project_id != project_id:
        return ""
    return " ".join(artifact.title.split())


def _artifact_line(version_id: str, label: str, status: str, title: str) -> str:
    phrase = _phrase(status)
    if status == "included" and title:
        return f"- 作品版本 {version_id}：{label}{phrase}，标题 {title}"
    return f"- 作品版本 {version_id}：{label}{phrase}"


def _phrase(status: str) -> str:
    if status == "included" or status == "allow":
        return "已纳入"
    if status == "tombstoned":
        return "已墓碑"
    if status == "missing":
        return "缺失"
    if status == "not_requested":
        return "未选择"
    return "未纳入"


def _item_status(gate: str) -> str:
    if gate == "tombstoned":
        return "tombstoned"
    if gate == "missing":
        return "missing"
    if gate == "allow":
        return "included"
    return "omitted"


def _tombstoned(session: Session, project_id: str, object_type: str, object_id: str) -> bool:
    found = session.scalar(
        select(PrivacyTombstone.id).where(
            PrivacyTombstone.project_id == project_id,
            PrivacyTombstone.object_type == object_type,
            PrivacyTombstone.object_id == object_id,
        )
    )
    return found is not None


def _sorted_ids(values: object) -> list[str]:
    if not isinstance(values, list):
        raise NotFoundOrHidden()
    ids = [item for item in values if isinstance(item, str)]
    if len(ids) != len(values):
        raise NotFoundOrHidden()
    return sorted(ids)


def _require_snapshot(payload: object) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            raise NotFoundOrHidden() from error
    elif isinstance(payload, dict):
        parsed = payload
    else:
        raise NotFoundOrHidden()
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise NotFoundOrHidden()
    if parsed.get("latest") is not False or parsed.get("format") != _FORMAT:
        raise NotFoundOrHidden()
    if type(parsed.get("include_originals")) is not bool:
        raise NotFoundOrHidden()
    if type(parsed.get("access_epoch")) is not int:
        raise NotFoundOrHidden()
    for key in (
        "export_job_id",
        "input_snapshot_id",
        "actor_id",
        "project_id",
        "audit_event_id",
    ):
        if not isinstance(parsed.get(key), str) or not parsed[key]:
            raise NotFoundOrHidden()
    manifest = parsed.get("source_manifest")
    scope = parsed.get("permission_scope")
    if not isinstance(manifest, dict) or not isinstance(scope, dict):
        raise NotFoundOrHidden()
    if scope.get("access_epoch") != parsed["access_epoch"]:
        raise NotFoundOrHidden()
    for key in ("page_version_ids", "artifact_version_ids", "evidence_anchor_ids"):
        if not isinstance(manifest.get(key), list):
            raise NotFoundOrHidden()
    return parsed


def _write_tree(root: Path, export_id: str, manifest_hash: str, files: dict[str, bytes]) -> None:
    if not is_storage_segment(export_id) or not is_storage_segment(manifest_hash):
        raise PathEscape(export_id)
    for name, payload in files.items():
        relative = f"{export_id}/{manifest_hash}/{name}"
        path = resolve_contained(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != payload:
                raise OSError("export package bytes differ")
            continue
        path.write_bytes(payload)


def _dump(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
