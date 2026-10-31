import pytest
from alembic import command
from conftest import alembic_config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.config import Settings

FAKE_KEY = "sk-test-not-a-real-key"


def test_configuration_rejects_enabled_provider_without_credentials():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_enabled=True, model_name="", deepseek_api_key=None)


def test_configuration_errors_and_repr_do_not_reveal_api_key():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, model_enabled=True, model_name="", deepseek_api_key=FAKE_KEY)
    assert FAKE_KEY not in str(error.value)
    assert FAKE_KEY not in repr(error.value)
    settings = Settings(_env_file=None, deepseek_api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in settings.model_dump_json()


def readiness(database_url):
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")
    return response.status_code, response.json()


@pytest.mark.integration
def test_health_and_missing_migration(database_url):
    assert readiness(database_url) == (
        503,
        {"status": "not_ready", "reason": "migration_required"},
    )


@pytest.mark.integration
def test_ready_after_real_alembic_migration(engine, database_url):
    status, body = readiness(database_url)
    assert status == 200
    assert body["scope"] == "foundation"


@pytest.mark.integration
def test_readyz_requires_migration_after_downgrade_to_base(engine, database_url):
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
    assert readiness(database_url) == (
        503,
        {"status": "not_ready", "reason": "migration_required"},
    )


@pytest.mark.integration
def test_readyz_rejects_unknown_revision(engine, database_url):
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))
    assert readiness(database_url) == (503, {"status": "not_ready", "reason": "schema_mismatch"})


@pytest.mark.integration
def test_readyz_rejects_stamped_revision_without_tables(bare_engine, database_url):
    with bare_engine.begin() as connection:
        command.stamp(alembic_config(connection), "head")
    assert readiness(database_url) == (
        503,
        {"status": "not_ready", "reason": "schema_incomplete"},
    )
