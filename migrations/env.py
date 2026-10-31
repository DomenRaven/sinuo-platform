from alembic import context

from sinuo.config import Settings
from sinuo.infrastructure.database import build_engine
from sinuo.infrastructure.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url") or Settings().database_url
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    supplied = config.attributes.get("connection")
    if supplied is not None:
        migrate(supplied)
        return
    settings = Settings()
    url = config.get_main_option("sqlalchemy.url") or settings.database_url
    engine = build_engine(url, settings.sqlite_busy_timeout_ms)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
