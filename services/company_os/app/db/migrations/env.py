"""Alembic environment for Company OS."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.db.models import Base
from app.db.session import _is_local as is_local, ensure_utc_sessions, normalize_db_url, resolve_database_url
from app.inbox.store import TABLES as INBOX_TABLES
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
assert all(table.metadata is target_metadata for table in INBOX_TABLES)

# Honour COMPANY_OS_DB_URL, then DATABASE_URL (existing ECS/RDS), then DB_URL.
# Without DATABASE_URL, a one-off `alembic upgrade head` on the live task
# would migrate the sqlite default in alembic.ini instead of RDS.
db_url = resolve_database_url()
if db_url:
    if is_local():
        config.set_main_option("sqlalchemy.url", normalize_db_url(db_url).replace("%", "%%"))
    else:
        config.set_main_option("sqlalchemy.url", normalize_db_url(db_url))


def _is_sqlite_url(url: str | None) -> bool:
    return bool(url) and url.startswith(("sqlite:", "sqlite+"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite_url(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_local_sqlite_migrations(connection: Connection) -> None:
    from app.db.local_sqlite import install_append_only_guards

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
        install_append_only_guards(connection)


def run_migrations_online() -> None:
    if is_local():
        connection = config.attributes.get("connection")
        if connection is not None and connection.dialect.name == "sqlite":
            run_local_sqlite_migrations(connection)
            return
        url = config.get_main_option("sqlalchemy.url")
        if _is_sqlite_url(url):
            from app.db.local_sqlite import ensure_sqlite_parent

            ensure_sqlite_parent(url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    ensure_utc_sessions(connectable)
    with connectable.connect() as connection:
        if is_local() and connection.dialect.name == "sqlite":
            with connection.begin():
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                run_local_sqlite_migrations(connection)
            return
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
            # Append-only triggers are installed by the initial migration itself
            # (and any later migration that recreates event_log). Do not install
            # here — autogenerate / empty DBs would fail before the table exists.


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
