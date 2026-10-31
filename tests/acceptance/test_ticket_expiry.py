"""Optional upload-ticket expiry. This file does not mark T05 done."""

import hashlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.application.assets.uploads import (
    AssetRejected,
    register_original,
    request_upload_ticket,
)
from sinuo.infrastructure.assets.records import UploadTicketRow
from sinuo.infrastructure.assets.staging import write_attempt_bytes
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ArtifactVersion, AuditEvent
from tests.acceptance.test_assets_derivatives import (
    ACCOUNT_ID,
    ARTIFACT_ID,
    NOW,
    PROJECT_ID,
    _seed,
    _ticket,
)

PAYLOAD = b"sinuo-expiry-bytes"


@pytest.fixture
def assets(engine):
    return engine


def _paths(tmp_path):
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    return attempts, objects


def _expiring_ticket(assets, *, declared_hash: str, size: int, expires_at: float):
    with write_uow(assets) as session:
        _seed(session)
        return request_upload_ticket(
            session,
            project_id=PROJECT_ID,
            artifact_id=ARTIFACT_ID,
            expected_size=size,
            declared_hash=declared_hash,
            media_type="model/gltf-binary",
            license_name="synthetic-license",
            sensitivity="project",
            ai_processing_allowed=0,
            account_id=ACCOUNT_ID,
            now=NOW,
            expires_at=expires_at,
        )


def _register(assets, ticket, attempts, objects, *, now: float):
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        return register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=now,
            converter_installed=False,
        )


def _version_and_audit_counts(assets) -> tuple[int, int]:
    with Session(assets) as session:
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
    assert type(versions) is int and type(audits) is int
    return versions, audits


@pytest.mark.integration
def test_omitted_expires_at_stays_null_and_registers(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    with Session(assets) as session:
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert row is not None
        assert row.expires_at is None
    outcome = _register(assets, ticket, attempts, objects, now=NOW + 1.0)
    assert outcome.registered is True
    assert outcome.artifact_version_id is not None
    assert _version_and_audit_counts(assets) == (1, 1)


@pytest.mark.integration
def test_expires_at_equal_to_now_leaves_no_version_or_audit(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    moment = NOW + 1.0
    ticket = _expiring_ticket(
        assets,
        declared_hash=digest,
        size=len(PAYLOAD),
        expires_at=moment,
    )
    outcome = _register(assets, ticket, attempts, objects, now=moment)
    assert outcome.registered is False
    assert outcome.reason == "ticket_expired"
    assert outcome.artifact_version_id is None
    assert _version_and_audit_counts(assets) == (0, 0)
    with Session(assets) as session:
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert row is not None
        assert row.status == "rejected"
        assert row.failure_reason == "ticket_expired"
        assert row.artifact_version_id is None
        assert row.closed_at == moment


@pytest.mark.integration
def test_expires_at_before_now_leaves_no_version_or_audit(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    ticket = _expiring_ticket(
        assets,
        declared_hash=digest,
        size=len(PAYLOAD),
        expires_at=NOW,
    )
    outcome = _register(assets, ticket, attempts, objects, now=NOW + 1.0)
    assert outcome.registered is False
    assert outcome.reason == "ticket_expired"
    assert outcome.artifact_version_id is None
    assert _version_and_audit_counts(assets) == (0, 0)
    with Session(assets) as session:
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert row is not None
        assert row.status == "rejected"
        assert row.failure_reason == "ticket_expired"
        assert row.artifact_version_id is None
        assert row.closed_at == NOW + 1.0


@pytest.mark.integration
def test_future_expires_at_still_registers(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts, objects = _paths(tmp_path)
    expires_at = NOW + 10.0
    ticket = _expiring_ticket(
        assets,
        declared_hash=digest,
        size=len(PAYLOAD),
        expires_at=expires_at,
    )
    outcome = _register(assets, ticket, attempts, objects, now=NOW + 1.0)
    assert outcome.registered is True
    assert outcome.reason is None
    assert outcome.artifact_version_id is not None
    assert _version_and_audit_counts(assets) == (1, 1)
    with Session(assets) as session:
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert row is not None
        assert row.expires_at == expires_at
        assert row.status == "registered"


@pytest.mark.integration
def test_non_float_expires_at_is_rejected_before_insert(assets):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    with pytest.raises(AssetRejected) as rejected:
        with write_uow(assets) as session:
            request_upload_ticket(
                session,
                project_id=PROJECT_ID,
                artifact_id=ARTIFACT_ID,
                expected_size=len(PAYLOAD),
                declared_hash=digest,
                media_type="model/gltf-binary",
                license_name="synthetic-license",
                sensitivity="project",
                ai_processing_allowed=0,
                account_id=ACCOUNT_ID,
                now=NOW,
                expires_at=1,  # type: ignore[arg-type]
            )
    assert rejected.value.reason == "expires_at"
    with Session(assets) as session:
        count = session.scalar(select(func.count()).select_from(UploadTicketRow))
    assert count == 0
