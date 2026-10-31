from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from sinuo.infrastructure.database import build_engine

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(connection) -> Config:
    configuration = Config(str(ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(ROOT / "migrations"))
    configuration.attributes["connection"] = connection
    return configuration


@pytest.fixture
def database_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def bare_engine(database_url):
    instance = build_engine(database_url)
    yield instance
    instance.dispose()


@pytest.fixture
def engine(bare_engine):
    with bare_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    return bare_engine
