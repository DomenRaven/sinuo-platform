"""Successful original registration writes one audit row in the caller transaction.

Hash mismatch, declared length mismatch, and a principal who did not create
the ticket do not. Reuse of the same bytes writes a second audit for the new
version and does not store a second physical original. This file does not
mark T05 done.
"""

import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.application.assets.uploads import register_original
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.assets.generations import StoredObjectRow
from sinuo.infrastructure.assets.records import UploadTicketRow
from sinuo.infrastructure.assets.staging import write_attempt_bytes
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ArtifactVersion, AuditEvent, Project
from tests.acceptance.test_assets_derivatives import (
    ACCOUNT_ID,
    ARTIFACT_ID,
    MEMBER_ID,
    NOW,
    PROJECT_ID,
    _account,
    _files_named,
    _membership,
    _principal,
    _project_grant,
    _seed,
    _ticket,
)

PAYLOAD = b"sinuo-audit-bytes"
_AUDIT_FIELDS = {
    "project_id",
    "artifact_id",
    "artifact_version_id",
    "content_hash",
    "generation_id",
    "byte_size",
}


@pytest.fixture
def assets(engine):
    return engine


def _paths(tmp_path):
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    return attempts, objects


def _open_ticket(assets, *, declared_hash: str, size: int):
    with write_uow(assets) as session:
        _seed(session)
        return _ticket(session, declared_hash=declared_hash, size=size)


@pytest.mark.integration
def test_successful_registration_writes_one_audit_row(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    ticket = _open_ticket(assets, declared_hash=digest, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.artifact_version_id is not None
    assert outcome.generation_id is not None
    with Session(assets) as session:
        rows = session.scalars(select(AuditEvent)).all()
        project = session.get(Project, PROJECT_ID)
        version = session.get(ArtifactVersion, outcome.artifact_version_id)
        assert len(rows) == 1
        audit = rows[0]
        assert project is not None and version is not None
        assert project.revision == 2
        assert audit.actor_id == ACCOUNT_ID
        assert audit.command == "artifact.version_registered"
        assert audit.subject_type == "artifact_version"
        assert audit.subject_id == outcome.artifact_version_id
        assert audit.project_revision == project.revision
        assert audit.project_revision == version.project_revision
        payload = json.loads(audit.payload)
        assert set(payload) == _AUDIT_FIELDS
        assert payload["project_id"] == PROJECT_ID
        assert payload["artifact_id"] == ARTIFACT_ID
        assert payload["artifact_version_id"] == outcome.artifact_version_id
        assert payload["content_hash"] == digest
        assert payload["generation_id"] == outcome.generation_id
        assert payload["byte_size"] == len(PAYLOAD)
        raw = audit.payload
        assert PAYLOAD.decode("ascii") not in raw
        assert str(attempts) not in raw
        assert str(objects) not in raw
        assert relative not in raw
        assert "\\" not in raw
        assert "/" not in raw


@pytest.mark.integration
def test_rejected_registration_leaves_the_audit_table_empty(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    declared = hashlib.sha256(b"sinuo-other-audit-bytes").hexdigest()
    assert declared != digest
    attempts, objects = _paths(tmp_path)
    ticket = _open_ticket(assets, declared_hash=declared, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt.part"
    written = write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
    assert outcome.registered is False
    assert outcome.reason == "declared_hash_mismatch"
    assert written.read_bytes() == PAYLOAD
    with Session(assets) as session:
        count = session.scalar(select(func.count()).select_from(AuditEvent))
        assert count == 0


@pytest.mark.integration
def test_reused_bytes_write_a_second_audit_for_the_new_version(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    first_ticket = _open_ticket(assets, declared_hash=digest, size=len(PAYLOAD))
    first_relative = f"{first_ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, first_relative, PAYLOAD)
    with write_uow(assets) as session:
        first = register_original(
            session,
            upload_id=first_ticket.upload_id,
            temp_root=attempts,
            relative_path=first_relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
    assert first.registered is True
    assert first.artifact_version_id is not None
    assert first.generation_id is not None
    with write_uow(assets) as session:
        second_ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    second_relative = f"{second_ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, second_relative, PAYLOAD)
    with write_uow(assets) as session:
        second = register_original(
            session,
            upload_id=second_ticket.upload_id,
            temp_root=attempts,
            relative_path=second_relative,
            objects_root=objects,
            now=NOW + 2.0,
            converter_installed=False,
        )
    assert second.registered is True
    assert second.artifact_version_id is not None
    assert second.artifact_version_id != first.artifact_version_id
    assert second.generation_id == first.generation_id
    assert second.object_key == first.object_key
    assert len(_files_named(objects, "original")) == 1
    with Session(assets) as session:
        rows = session.scalars(select(AuditEvent)).all()
        stored = session.scalar(select(func.count()).select_from(StoredObjectRow))
        versions = session.scalars(select(ArtifactVersion)).all()
        project = session.get(Project, PROJECT_ID)
    assert stored == 1
    assert len(versions) == 2
    assert {version.id for version in versions} == {
        first.artifact_version_id,
        second.artifact_version_id,
    }
    assert len(rows) == 2
    second_audit = next(row for row in rows if row.subject_id == second.artifact_version_id)
    assert {row.subject_id for row in rows} == {
        first.artifact_version_id,
        second.artifact_version_id,
    }
    assert project is not None
    assert second_audit.actor_id == ACCOUNT_ID
    assert second_audit.command == "artifact.version_registered"
    assert second_audit.subject_type == "artifact_version"
    assert second_audit.project_revision == project.revision
    payload = json.loads(second_audit.payload)
    assert set(payload) == _AUDIT_FIELDS
    assert payload["project_id"] == PROJECT_ID
    assert payload["artifact_id"] == ARTIFACT_ID
    assert payload["artifact_version_id"] == second.artifact_version_id
    assert payload["content_hash"] == digest
    assert payload["generation_id"] == second.generation_id
    assert payload["byte_size"] == len(PAYLOAD)


@pytest.mark.integration
def test_declared_length_mismatch_writes_no_audit_and_no_version(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    declared_size = len(PAYLOAD) - 1
    attempts, objects = _paths(tmp_path)
    ticket = _open_ticket(assets, declared_hash=digest, size=declared_size)
    relative = f"{ticket.upload_id}/attempt.part"
    written = write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
    assert outcome.registered is False
    assert outcome.reason == "expected_size_mismatch"
    assert outcome.artifact_version_id is None
    assert written.read_bytes() == PAYLOAD
    with Session(assets) as session:
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        assert audits == 0
        assert versions == 0


@pytest.mark.integration
def test_ticket_owner_mismatch_writes_no_audit_and_leaves_the_ticket_open(
    assets, tmp_path
):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    ticket = _open_ticket(assets, declared_hash=digest, size=len(PAYLOAD))
    with write_uow(assets) as session:
        _account(session, MEMBER_ID, "synthetic-member")
        session.flush()
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _project_grant(
            session,
            grant_id="synthetic-grant-member-upload",
            project_id=PROJECT_ID,
            account_id=MEMBER_ID,
            action=Action.ARTIFACT_UPLOAD,
        )
    relative = f"{ticket.upload_id}/attempt.part"
    written = write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
            principal=_principal(MEMBER_ID),
            authorizer=SessionAuthorizer(session),
        )
    assert outcome.registered is False
    assert outcome.reason == "ticket_owner"
    assert outcome.artifact_version_id is None
    assert written.read_bytes() == PAYLOAD
    with Session(assets) as session:
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert audits == 0
        assert versions == 0
        assert row is not None
        assert row.status == "open"
        assert row.artifact_version_id is None
        assert row.closed_at is None
