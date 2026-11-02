"""Fixed product-design stages on project creation. Titles stay on the synthetic- prefix."""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.application.versions.pages import create_parallel_page
from sinuo.application.versions.projects import CreatedProject, create_project
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Page,
    PageEdge,
    Stage,
    StageConfigVersion,
)

ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = 1_700_000_100.0
EXPECTED_NAMES = (
    "任务与情境",
    "用户与场景研究",
    "问题及机会定义",
    "设计要求",
    "概念探索",
    "深化与原型",
    "测试与迭代",
    "表达与复盘",
)


def _ready(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            Account(
                id=ACCOUNT_ID,
                login_name="synthetic-template-creator",
                display_name="synthetic-template-creator",
                password_hash="$argon2id$synthetic-hash",
                status="active",
                auth_epoch=1,
                can_create_project=1,
                created_at=NOW,
                disabled_at=None,
            )
        )


def _open(session: Session, *, template_key: str) -> CreatedProject:
    return create_project(
        session,
        account_id=ACCOUNT_ID,
        title="synthetic-product",
        brief="synthetic-brief",
        template_key=template_key,
        page_title="synthetic-home",
        now=NOW,
    )


def _head_rows(session: Session, project_id: str) -> list[tuple[int, str, str]]:
    rows = session.execute(
        select(
            StageConfigVersion.position,
            StageConfigVersion.goal_json,
            Stage.id,
        )
        .join(Stage, Stage.id == StageConfigVersion.stage_id)
        .where(
            Stage.project_id == project_id,
            Stage.head_config_version_id == StageConfigVersion.id,
        )
        .order_by(StageConfigVersion.position, Stage.id)
    ).all()
    return [(row.position, row.goal_json, row.id) for row in rows]


def test_create_project_writes_eight_named_stages_and_home_on_the_first(engine):
    _ready(engine)
    with write_uow(engine) as session:
        created = _open(session, template_key="synthetic-not-a-library")
        other = _open(session, template_key="synthetic-other-key")
    with Session(engine) as session:
        rows = _head_rows(session, created.project_id)
        assert [position for position, _goal, _stage_id in rows] == list(range(8))
        names = []
        for _position, goal_json, _stage_id in rows:
            payload = json.loads(goal_json)
            assert payload["goal"] == "synthetic-product"
            names.append(payload["name"])
        assert tuple(names) == EXPECTED_NAMES
        assert created.stage_id == rows[0][2]
        assert names[0] == "任务与情境"
        page = session.get(Page, created.page_id)
        assert page is not None
        assert page.stage_id == created.stage_id
        assert page.title == "synthetic-home"
        pages = session.scalars(select(Page).where(Page.project_id == created.project_id)).all()
        assert [item.stage_id for item in pages] == [created.stage_id]
        assert session.scalar(select(func.count()).select_from(PageEdge)) == 0
        other_names = tuple(
            json.loads(goal_json)["name"]
            for _position, goal_json, _stage_id in _head_rows(session, other.project_id)
        )
        assert other_names == EXPECTED_NAMES


def test_second_page_in_the_same_stage_adds_no_edge(engine):
    _ready(engine)
    with write_uow(engine) as session:
        created = _open(session, template_key="synthetic-not-a-library")
    with write_uow(engine) as session:
        stages_before = session.scalar(select(func.count()).select_from(Stage))
        second = create_parallel_page(
            session,
            project_id=created.project_id,
            stage_id=created.stage_id,
            account_id=ACCOUNT_ID,
            title="synthetic-parallel",
            expected_revision=1,
            now=NOW,
        )
    with Session(engine) as session:
        assert stages_before == 8
        assert session.scalar(select(func.count()).select_from(Stage)) == 8
        assert session.scalar(select(func.count()).select_from(PageEdge)) == 0
        next_edges = session.scalar(
            select(func.count()).select_from(PageEdge).where(PageEdge.kind == "next")
        )
        assert next_edges == 0
        home = session.get(Page, created.page_id)
        extra = session.get(Page, second.page_id)
        assert home is not None
        assert extra is not None
        assert home.id != extra.id
        assert home.stage_id == extra.stage_id == created.stage_id
        home_name = session.scalar(
            select(StageConfigVersion.goal_json).where(
                StageConfigVersion.id == created.stage_config_version_id
            )
        )
        assert isinstance(home_name, str)
        assert json.loads(home_name)["name"] == "任务与情境"
