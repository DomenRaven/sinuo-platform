from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from sinuo.domain.schema_types import login_name_for_sql


def build_engine(database_url: str, busy_timeout_ms: int = 5000) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("This scaffold supports SQLite; add a tested adapter before another DB")
    if url.database and url.database != ":memory:":
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False}, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
        dbapi_connection.create_function(
            "sinuo_login_name_canonical",
            1,
            login_name_for_sql,
            deterministic=True,
        )

    return engine


@contextmanager
def write_uow(engine: Engine) -> Iterator[Session]:
    """Short SQLite write unit; model, network and file conversion stay outside it.

    The unit owns commit and rollback. Standard Session/Connection commits inside
    the block are rejected. Handlers must not issue raw transaction-control SQL or
    access DBAPI directly; this is a trusted-code seam, not a Python sandbox.
    """
    with Session(engine, expire_on_commit=False) as session:
        owner_committing = False

        def reject_inner_commit(_session: Session) -> None:
            if not owner_committing:
                raise RuntimeError("write_uow owns the commit; do not commit inside the unit")

        event.listen(session, "before_commit", reject_inner_commit)
        connection = session.connection()

        def reject_connection_commit(_connection):
            if not owner_committing:
                raise RuntimeError("write_uow owns the commit; do not commit its connection")

        event.listen(connection, "commit", reject_connection_commit)
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            transaction = session.get_transaction()
            connection_transaction = connection.get_transaction()
            yield session
            if (
                session.get_transaction() is not transaction
                or connection.get_transaction() is not connection_transaction
                or not connection_transaction.is_active
            ):
                raise RuntimeError("write_uow transaction was ended inside the unit")
            owner_committing = True
            session.commit()
        except BaseException:
            session.rollback()
            raise
