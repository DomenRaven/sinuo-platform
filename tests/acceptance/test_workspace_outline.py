"""Workspace reads: visible projects, stored stages, and the activity draft."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.application.auth.acl import ensure_membership, insert_grant
from sinuo.domain.auth.roles import grants_for_template
from sinuo.domain.public_contract import CSRF_HEADER, RoleTemplate
from sinuo.infrastructure.database import write_uow
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PAGE_A,
    PAGE_B_TITLE,
    PROJECT_A,
    PROJECT_A_TITLE,
    PROJECT_B_TITLE,
    TEAM_ID,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
TITLE = "synthetic-outline-project"
HIDDEN = "synthetic-hidden-page"
STAGE_NAMES = (
    "任务与情境",
    "用户与场景研究",
    "问题及机会定义",
    "设计要求",
    "概念探索",
    "深化与原型",
    "测试与迭代",
    "表达与复盘",
)


def _login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 204, response.text
    current = client.get("/api/v1/session")
    assert current.status_code == 200, current.text
    return current.json()["data"]["csrf_token"]


def _logout(client: TestClient, csrf: str) -> None:
    response = client.delete(
        "/api/v1/session",
        headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
    )
    assert response.status_code == 204, response.text


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def _counts(engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        pages = connection.execute(text("SELECT count(*) FROM pages")).scalar_one()
        versions = connection.execute(text("SELECT count(*) FROM page_versions")).scalar_one()
        drafts = connection.execute(text("SELECT count(*) FROM page_drafts")).scalar_one()
    return int(pages), int(versions), int(drafts)


def _grant_home(engine, project_id: str, page_id: str) -> None:
    with write_uow(engine) as session:
        ensure_membership(session, project_id=project_id, account_id=TEAM_ID, now=1_761_800_100.0)
        for spec in grants_for_template(RoleTemplate.TEAM_MEMBER, (page_id,)):
            insert_grant(
                session,
                project_id=project_id,
                account_id=TEAM_ID,
                spec=spec,
                created_by_account_id=LEAD_ID,
                now=1_761_800_100.0,
            )


def test_reads_follow_membership_and_do_not_write(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        created = client.post(
            "/api/v1/projects",
            headers=_headers(csrf, "synthetic-outline-create"),
            json={"title": TITLE},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["data"]["project_id"]
        home_id = created.json()["data"]["page_id"]
        stage_id = created.json()["data"]["stage_id"]

        listed = client.get("/api/v1/projects")
        assert listed.status_code == 200, listed.text
        titles = {item["title"] for item in listed.json()["data"]["projects"]}
        assert titles == {PROJECT_A_TITLE, TITLE}
        assert PROJECT_B_TITLE not in listed.text

        stages = client.get(f"/api/v1/projects/{project_id}/stages")
        assert stages.status_code == 200, stages.text
        body = stages.json()["data"]
        assert body["title"] == TITLE
        assert body["revision"] == 1
        assert [item["name"] for item in body["stages"]] == list(STAGE_NAMES)
        assert [item["position"] for item in body["stages"]] == list(range(8))
        assert body["stages"][0]["stage_id"] == stage_id
        assert body["stages"][0]["pages"][0]["page_id"] == home_id
        assert body["stages"][0]["pages"][0]["title"] == TITLE
        assert body["stages"][0]["has_hidden_pages"] is False
        assert all(item["pages"] == [] for item in body["stages"][1:])

        draft = client.get(f"/api/v1/pages/{home_id}/draft")
        assert draft.status_code == 200, draft.text
        page = draft.json()["data"]
        assert page["draft"]["content_revision"] == 1
        assert page["head_version_no"] == 1
        assert page["nodes"] == []
        assert page["nodes_readable"] is True

        seeded = client.get(f"/api/v1/pages/{PAGE_A}/draft")
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["data"]["draft"] is None
        assert seeded.json()["data"]["nodes"] == []
        assert "synthetic-node-sensitive" not in seeded.text
        older = client.get(f"/api/v1/projects/{PROJECT_A}/stages")
        assert older.json()["data"]["stages"][0]["name"] is None
        assert STAGE_NAMES[-1] not in older.text

        before = _counts(engine)
        again = client.get(f"/api/v1/projects/{project_id}/stages")
        home = client.get(f"/api/v1/pages/{home_id}/draft")
        assert again.status_code == 200
        assert home.status_code == 200
        assert _counts(engine) == before

        hidden = client.post(
            f"/api/v1/projects/{project_id}/pages",
            headers=_headers(csrf, "synthetic-outline-parallel"),
            json={"stage_id": stage_id, "title": HIDDEN, "expected_revision": 1},
        )
        assert hidden.status_code == 201, hidden.text
        hidden_id = hidden.json()["data"]["page_id"]
        seen = client.get(f"/api/v1/projects/{project_id}/stages")
        assert seen.json()["data"]["revision"] == 2
        visible = [page["title"] for page in seen.json()["data"]["stages"][0]["pages"]]
        assert visible == [TITLE, HIDDEN]
        assert seen.json()["data"]["stages"][0]["has_hidden_pages"] is False
        after_write = _counts(engine)
        assert client.get(f"/api/v1/projects/{project_id}/stages").status_code == 200
        assert _counts(engine) == after_write
        _logout(client, csrf)

        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        before_grant = client.get("/api/v1/projects")
        assert TITLE not in before_grant.text
        assert PROJECT_B_TITLE not in before_grant.text
        _logout(client, team)

    _grant_home(engine, project_id, home_id)

    with TestClient(app) as client:
        csrf = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        limited = client.get(f"/api/v1/projects/{project_id}/stages")
        assert limited.status_code == 200, limited.text
        first = limited.json()["data"]["stages"][0]
        assert [page["title"] for page in first["pages"]] == [TITLE]
        assert first["has_hidden_pages"] is True
        assert HIDDEN not in limited.text
        assert hidden_id not in limited.text
        assert "hidden_count" not in limited.text
        blocked = client.get(f"/api/v1/pages/{hidden_id}/draft")
        assert blocked.status_code == 404
        assert blocked.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert HIDDEN not in blocked.text
        opened = client.get(f"/api/v1/pages/{home_id}/draft")
        assert opened.status_code == 200
        assert opened.json()["data"]["title"] == TITLE
        _logout(client, csrf)

        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        foreign = client.get("/api/v1/projects")
        assert {item["title"] for item in foreign.json()["data"]["projects"]} == {PROJECT_B_TITLE}
        missing = client.get(f"/api/v1/projects/{project_id}/stages")
        assert missing.status_code == 404
        assert TITLE not in missing.text
        assert PAGE_B_TITLE not in foreign.text
        _logout(client, other)

        anonymous = client.get("/api/v1/projects")
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"


def test_desk_shell_and_read_routes_are_mounted(database_url):
    app = create_app(identity_settings(database_url))
    paths = app.openapi()["paths"]
    assert "get" in paths["/api/v1/projects"]
    assert "get" in paths["/api/v1/projects/{project_id}/stages"]
    assert "get" in paths["/api/v1/pages/{page_id}/draft"]
    with TestClient(app) as client:
        home = client.get("/")
        shelf = client.get("/projects")
        page = client.get("/projects/synthetic-id/pages/synthetic-page")
        assert home.status_code == shelf.status_code == page.status_code == 200
        assert "text/html" in home.headers["content-type"]
        assert home.text == shelf.text == page.text
        assert "/api/v1/projects" in home.text
        assert "/stages" in home.text
        assert "/api/v1/pages/" in home.text
        assert "表达与复盘" not in home.text
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "alive"
