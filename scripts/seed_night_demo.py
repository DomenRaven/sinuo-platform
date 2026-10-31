"""Seed a temporary synthetic project and print the fake-chain view URL.

The project title is 夜间取物辅助产品. Every row is marked synthetic. This
calls ``run_fake_chain`` and does not call ``consume_candidate``. It does not
enable a live model and it does not write ``var/sinuo.db``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.api.app import create_app
from sinuo.application.agents.fake_chain import run_fake_chain
from sinuo.application.auth.acl import ensure_membership, insert_grant
from sinuo.application.auth.passwords import canonical_login_name, hash_password
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.roles import grants_for_template
from sinuo.domain.public_contract import RoleTemplate
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    Candidate,
    EvidenceAnchor,
    Page,
    PageVersion,
    Project,
    Stage,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_762_000_000.0
LOGIN_NAME = "synthetic-night"
PASSWORD = "synthetic-night-pass"
ACCOUNT_ID = "55555555-5555-4555-8555-555555555555"
PROJECT_ID = "synthetic-night-pickup"
STAGE_ID = "synthetic-night-stage"
PAGE_ID = "synthetic-night-page"
PAGE_VERSION_ID = "synthetic-night-page-v1"
ARTIFACT_ID = "synthetic-night-art"
ARTIFACT_VERSION_ID = "synthetic-night-art-v1"
ANCHOR_ID = "synthetic-night-anchor"
PROJECT_TITLE = "夜间取物辅助产品"
PAGE_TITLE = "夜间取物需求"
QUERY = "夜间取物"
EXCERPT = "夜间取物时要少挪开其他物品"
INTENT = "确认夜间取物是否要少挪开其他物品"
PAGE_BODY = "synthetic=true。虚构观察，不是真实用户研究。希望夜间取物时少挪开其他物品。"
BRIEF = "synthetic=true。虚构课题，不是真实用户研究。没有真实模型。"
PUBLIC_URL = "http://127.0.0.1:8000"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(QUERY) > 40 or not QUERY or QUERY not in EXCERPT:
        raise SystemExit("query must stay inside the excerpt and the search limit")
    database_path = (
        ROOT / "_施工文件" / "2026-11-01_controller" / "99_临时文件" / "night-demo.sqlite"
    )
    database_url = "sqlite:///" + database_path.resolve().as_posix()
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        model_enabled=False,
        public_url=PUBLIC_URL,
        allowed_origins=PUBLIC_URL,
    )
    _replace_database(database_path)
    print("正在写入口令哈希", file=sys.stderr)
    password_hash = hash_password(settings, PASSWORD)
    engine = build_engine(database_url, settings.sqlite_busy_timeout_ms)
    try:
        with engine.begin() as connection:
            _upgrade(connection)
        _seed(engine, password_hash)
        result = run_fake_chain(
            engine,
            Principal(ACCOUNT_ID, "synthetic-night-session", 1),
            PROJECT_ID,
            intent=INTENT,
            query=QUERY,
            page_version_id=PAGE_VERSION_ID,
            now=NOW,
        )
    finally:
        engine.dispose()
    view_path = (
        f"/api/v1/projects/{PROJECT_ID}/agent-runs/{result.parent_run_id}/fake-chain/view"
    )
    _check_page(settings, view_path, result.candidate_id)
    report = _report(
        database_path.resolve(),
        database_url,
        result.parent_run_id,
        result.candidate_id,
        view_path,
    )
    print(report)
    report_path = database_path.with_name("night-demo.txt")
    report_path.write_text(report + "\n", encoding="utf-8")


def _replace_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            raise SystemExit(f"无法重写临时库 {candidate}: {exc}") from exc


def _upgrade(connection) -> None:
    configuration = Config(str(ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(ROOT / "migrations"))
    configuration.attributes["connection"] = connection
    command.upgrade(configuration, "head")


def _seed(engine, password_hash: str) -> None:
    login_name = canonical_login_name(LOGIN_NAME)
    body_hash = hashlib.sha256(PAGE_BODY.encode("utf-8")).hexdigest()
    excerpt_hash = hashlib.sha256(EXCERPT.encode("utf-8")).hexdigest()
    version_hash = hashlib.sha256(ARTIFACT_VERSION_ID.encode("utf-8")).hexdigest()
    content_json = json.dumps({"body": PAGE_BODY}, ensure_ascii=False)
    locator_json = json.dumps(
        {"excerpt": EXCERPT, "page": 1},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT_ID,
                title=PROJECT_TITLE,
                brief=BRIEF,
                template_key=None,
                revision=1,
                access_epoch=1,
                created_at=NOW,
            )
        )
        session.add(
            Account(
                id=ACCOUNT_ID,
                login_name=login_name,
                display_name="合成演示账号",
                password_hash=password_hash,
                status="active",
                auth_epoch=1,
                can_create_project=1,
                created_at=NOW,
                disabled_at=None,
            )
        )
        session.flush()
        ensure_membership(
            session, project_id=PROJECT_ID, account_id=ACCOUNT_ID, now=NOW
        )
        session.add(
            Stage(
                id=STAGE_ID,
                project_id=PROJECT_ID,
                head_config_version_id=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Artifact(
                id=ARTIFACT_ID,
                project_id=PROJECT_ID,
                title="合成观察记录",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ArtifactVersion(
                id=ARTIFACT_VERSION_ID,
                project_id=PROJECT_ID,
                artifact_id=ARTIFACT_ID,
                version_no=1,
                content_hash=version_hash,
                size_bytes=len(EXCERPT.encode("utf-8")),
                media_type="text/plain",
                object_key="synthetic/night-observation",
                license="synthetic",
                sensitivity="project",
                ai_processing_allowed=0,
                project_revision=1,
                created_by_account_id=ACCOUNT_ID,
                created_at=NOW,
            )
        )
        session.add(
            Page(
                id=PAGE_ID,
                project_id=PROJECT_ID,
                stage_id=STAGE_ID,
                head_version_id=None,
                status="active",
                title=PAGE_TITLE,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            PageVersion(
                id=PAGE_VERSION_ID,
                project_id=PROJECT_ID,
                page_id=PAGE_ID,
                version_no=1,
                parent_version_id=None,
                content_json=content_json,
                content_hash=body_hash,
                schema_version=1,
                project_revision=1,
                created_by_account_id=ACCOUNT_ID,
                created_at=NOW,
            )
        )
        session.flush()
        page = session.get(Page, PAGE_ID)
        if page is None:
            raise RuntimeError("page")
        page.head_version_id = PAGE_VERSION_ID
        session.add(
            EvidenceAnchor(
                id=ANCHOR_ID,
                project_id=PROJECT_ID,
                artifact_version_id=ARTIFACT_VERSION_ID,
                locator_type="text_range",
                locator_json=locator_json,
                excerpt_hash=excerpt_hash,
                extractor_version="synthetic-1",
                visibility="project",
                sensitivity="project",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=ACCOUNT_ID,
                created_at=NOW,
            )
        )
        session.flush()
        for spec in grants_for_template(RoleTemplate.LEAD_DESIGNER):
            insert_grant(
                session,
                project_id=PROJECT_ID,
                account_id=ACCOUNT_ID,
                spec=spec,
                created_by_account_id=ACCOUNT_ID,
                now=NOW,
            )


def _check_page(settings: Settings, view_path: str, candidate_id: str) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        anonymous = client.get(view_path)
        login = client.post(
            "/api/v1/session",
            json={"login_name": LOGIN_NAME, "password": PASSWORD},
            headers={"Origin": PUBLIC_URL},
        )
        page = client.get(view_path)
        body = client.get(view_path.removesuffix("/view"))
    if ready.status_code != 200:
        raise SystemExit(f"readyz {ready.status_code} {ready.text}")
    if anonymous.status_code != 401 or PROJECT_TITLE in anonymous.text:
        raise SystemExit(f"anonymous view {anonymous.status_code}")
    if login.status_code != 204:
        raise SystemExit(f"login {login.status_code} {login.text}")
    if page.status_code != 200:
        raise SystemExit(f"view {page.status_code} {page.text}")
    required = (
        "待确认",
        EXCERPT,
        PAGE_TITLE,
        "FakeModelProvider",
        "不是真实模型",
        "confirm-local",
        "reject-local",
    )
    missing = [item for item in required if item not in page.text]
    if missing or "consume_candidate" in page.text:
        raise SystemExit(f"view text missing {missing}")
    payload = body.json()
    if body.status_code != 200 or payload["data"]["candidate_status"] != "pending":
        raise SystemExit(f"json {body.status_code} {body.text}")
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        with Session(engine) as session:
            status = session.scalar(select(Candidate.status).where(Candidate.id == candidate_id))
            count = int(session.scalar(select(func.count()).select_from(Candidate)) or 0)
    finally:
        engine.dispose()
    if status != "pending" or count != 1:
        raise SystemExit(f"candidate {status} count {count}")


def _report(
    database_path: Path,
    database_url: str,
    parent_run_id: str,
    candidate_id: str,
    view_path: str,
) -> str:
    view_url = PUBLIC_URL + view_path
    return "\n".join(
        (
            f"临时库: {database_path}",
            f"database_url: {database_url}",
            f"课题: {PROJECT_TITLE}",
            "标记: synthetic=true。虚构数据，不是真实用户研究。",
            f"登录名: {LOGIN_NAME}",
            f"口令: {PASSWORD}",
            f"父运行: {parent_run_id}",
            f"候选: {candidate_id} status=pending",
            f"页面: {view_url}",
            "",
            "在仓库根的同一个 PowerShell 里启动，不要另开 live model：",
            f'$env:SINUO_DATABASE_URL = "{database_url}"',
            '$env:SINUO_MODEL_ENABLED = "false"',
            f'$env:SINUO_PUBLIC_URL = "{PUBLIC_URL}"',
            f'$env:SINUO_ALLOWED_ORIGINS = "{PUBLIC_URL}"',
            ".\\.venv\\Scripts\\uvicorn.exe sinuo.api.app:create_app "
            "--factory --host 127.0.0.1 --port 8000",
            "",
            "浏览器打开上面的页面。未登录时是 401。在该页控制台执行后刷新：",
            (
                "fetch('/api/v1/session',{method:'POST',credentials:'include',"
                "headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{login_name:'{LOGIN_NAME}',password:'{PASSWORD}'}})}})"
                ".then(()=>location.reload())"
            ),
            "",
            "页面上的待确认是这条 pending 候选。确认和拒绝只改本页文字，不调用 consume_candidate。",
        )
    )


if __name__ == "__main__":
    main()
