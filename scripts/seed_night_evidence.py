"""给 var/sinuo.db 的「夜间取物」项目喂一份演示证据并重建索引。

只写 synthetic 演示数据，不调用模型，不读密钥。目标是让 search_evidence
在「问下一步」时能命中，使过程检查可以基于证据给出阶段建议。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid

from sqlalchemy import select

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.retrieval.search import rebuild_evidence_index, search_evidence
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.domain_models import Artifact, ArtifactVersion, EvidenceAnchor

PROJECT_ID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
EXCERPT = "夜间取物：床边没有灯，用户要先摸到开关才能开灯，黑暗中容易碰倒床头的水杯或手机"
QUERY = "夜间取物"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings(_env_file=None)
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        now = time.time()
        with write_uow(engine) as session:
            existing = session.scalar(select(Artifact).where(Artifact.project_id == PROJECT_ID))
            if existing is not None:
                print(f"已存在作品 {existing.id}，跳过喂证据")
            else:
                artifact_id = str(uuid.uuid4())
                version_id = str(uuid.uuid4())
                anchor_id = str(uuid.uuid4())
                session.add(
                    Artifact(
                        id=artifact_id,
                        project_id=PROJECT_ID,
                        title="夜间取物观察记录",
                        created_at=now,
                    )
                )
                session.add(
                    ArtifactVersion(
                        id=version_id,
                        project_id=PROJECT_ID,
                        artifact_id=artifact_id,
                        version_no=1,
                        content_hash=_sha(EXCERPT),
                        size_bytes=len(EXCERPT.encode("utf-8")),
                        media_type="text/plain",
                        object_key=f"synthetic/{version_id}",
                        license="synthetic",
                        sensitivity="project",
                        ai_processing_allowed=1,
                        project_revision=1,
                        created_by_account_id=ACCOUNT_ID,
                        created_at=now,
                    )
                )
                locator_json = json.dumps(
                    {"excerpt": EXCERPT, "page": 1},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                session.add(
                    EvidenceAnchor(
                        id=anchor_id,
                        project_id=PROJECT_ID,
                        artifact_version_id=version_id,
                        locator_type="text_range",
                        locator_json=locator_json,
                        excerpt_hash=_sha(EXCERPT),
                        extractor_version="synthetic-1",
                        visibility="project",
                        sensitivity="project",
                        supersedes_id=None,
                        schema_version=1,
                        created_by_account_id=ACCOUNT_ID,
                        created_at=now,
                    )
                )
                session.flush()
                inserted = rebuild_evidence_index(session)
                print(
                    f"喂入证据: artifact={artifact_id} version={version_id} "
                    f"anchor={anchor_id} 索引新增 {inserted} 行"
                )
        with write_uow(engine) as session:
            principal = Principal(ACCOUNT_ID, "seed-session", 1)
            authorizer = SessionAuthorizer(session)
            found = search_evidence(session, authorizer, principal, PROJECT_ID, QUERY, 12)
        print(f"search_evidence('{QUERY}') -> {len(found.hits)} hits, counts={found.counts}")
        for hit in found.hits:
            print(f"  hit: version={hit.artifact_version_id} excerpt={hit.locator.get('excerpt')}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
