"""Finish export jobs that are queued and already due.

The read lists those ids. ``run_export_job`` claims each one. A queued export
whose ``available_at`` is still ahead of the database clock stays queued.
Other job kinds are left alone. This module does not register an export and
does not build the package itself.
"""

import math
from pathlib import Path

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from sinuo.application.exports.package import ExportRun, run_export_job
from sinuo.infrastructure.domain_models import Job

_CLOCK = text("SELECT (julianday('now') - 2440587.5) * 86400.0")


def drain_export_jobs(
    engine: Engine,
    exports_root: Path,
    objects_root: Path | None,
    derived_root: Path | None,
) -> list[ExportRun]:
    """Claim each due queued export by calling the existing runner."""

    finished: list[ExportRun] = []
    for export_id in _due_export_ids(engine):
        finished.append(
            run_export_job(
                engine,
                export_id,
                exports_root=exports_root,
                objects_root=objects_root,
                derived_root=derived_root,
            )
        )
    return finished


def _due_export_ids(engine: Engine) -> list[str]:
    with Session(engine) as session:
        moment = _database_moment(session)
        found = session.scalars(
            select(Job.id)
            .where(
                Job.job_kind == "export",
                Job.status == "queued",
                Job.available_at <= moment,
            )
            .order_by(Job.created_at, Job.id)
        ).all()
    return [item for item in found if isinstance(item, str)]


def _database_moment(session: Session) -> float:
    value = session.scalar(_CLOCK)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("database clock is unavailable")
    moment = float(value)
    if not math.isfinite(moment):
        raise RuntimeError("database clock is unavailable")
    return moment
