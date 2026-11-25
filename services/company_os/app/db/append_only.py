"""Dialect-aware append-only UPDATE/DELETE triggers for create_all schemas.

Alembic migrations install these for a migrated database. Tests and the
``state.db()`` boot path use ``create_all``, which does not emit triggers, so
this helper must be able to install the same rules on both SQLite and Postgres.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.db.models import APPEND_ONLY_TABLES


def install_append_only_triggers(engine: Engine) -> None:
    """Idempotently install no-UPDATE / no-DELETE triggers on append-only tables."""
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            # Every process installs these on first use; concurrent CREATE OR REPLACE FUNCTION
            # fails with "tuple concurrently updated", so serialise installers across processes.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('supymem_append_only_triggers'))"))
        for table in sorted(APPEND_ONLY_TABLES):
            if dialect == "sqlite":
                _install_sqlite(conn, table)
            elif dialect == "postgresql":
                _install_postgres(conn, table)


def _install_sqlite(conn: Connection, table: str) -> None:
    conn.execute(
        text(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
        )
    )
    conn.execute(
        text(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
        )
    )


def _install_postgres(conn: Connection, table: str) -> None:
    conn.execute(
        text(
            f"CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger AS $$"
            f" BEGIN RAISE EXCEPTION '{table} is append-only'; END; "
            f"$$ LANGUAGE plpgsql;"
        )
    )
    conn.execute(
        text(
            f"CREATE OR REPLACE TRIGGER {table}_no_update "
            f"BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {table}_append_only();"
        )
    )
    conn.execute(
        text(
            f"CREATE OR REPLACE TRIGGER {table}_no_delete "
            f"BEFORE DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {table}_append_only();"
        )
    )
