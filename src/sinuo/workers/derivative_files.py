"""Write one derivative attempt outside the database transaction.

Bytes land only at ``root/jobs/<job_id>/<fencing_token>/result``. Publishing that
relative path is ``finalize_job`` inside the caller's ``write_uow``.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment
from sinuo.infrastructure.assets.staging import write_attempt_bytes
from sinuo.workers.jobs import JobPublication, finalize_job


def write_derivative_attempt(
    root: Path,
    job_id: str,
    fencing_token: int,
    payload: bytes,
) -> str:
    """Store the attempt file. This function does not open a database session."""

    relative = _attempt_relative(job_id, fencing_token)
    write_attempt_bytes(root, relative, payload)
    return relative


def publish_derivative_attempt(
    session: Session,
    *,
    job_id: str,
    owner: str,
    fencing_token: int,
    input_hash: str,
    consumer_name: str,
    effect_kind: str,
) -> JobPublication:
    """Register the attempt path. The caller owns the write transaction."""

    return finalize_job(
        session,
        job_id=job_id,
        owner=owner,
        fencing_token=fencing_token,
        input_hash=input_hash,
        output_ref=_attempt_relative(job_id, fencing_token),
        consumer_name=consumer_name,
        effect_kind=effect_kind,
    )


def _attempt_relative(job_id: str, fencing_token: int) -> str:
    if type(fencing_token) is not int or fencing_token < 1:
        raise PathEscape(job_id if isinstance(job_id, str) else "")
    token = str(fencing_token)
    if not is_storage_segment(job_id) or not is_storage_segment(token):
        raise PathEscape(job_id if isinstance(job_id, str) else "")
    return f"jobs/{job_id}/{token}/result"
