"""Postgres append-only triggers that the SQLite-only initial migration missed.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-19

``event_log`` triggers in ``bb8ea93e5a0f`` are SQLite-only. ``ops_activity`` is
in ``APPEND_ONLY_TABLES`` but never received triggers on either dialect.
Hash-chained tables and the other append-only tables already have Postgres
branches in later migrations; this revision only adds what is missing so we
do not drop-and-recreate live triggers on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_triggers(table: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE TRIGGER {table}_no_update
        BEFORE UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {table}_append_only();
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {table}_append_only();
        """
    )


def _sqlite_triggers(table: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_update
        BEFORE UPDATE ON {table} BEGIN
          SELECT RAISE(ABORT, '{table} is append-only');
        END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_delete
        BEFORE DELETE ON {table} BEGIN
          SELECT RAISE(ABORT, '{table} is append-only');
        END;
        """
    )


def _drop_pg(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")


def _drop_sqlite(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        _pg_triggers("event_log")
        _pg_triggers("ops_activity")
    elif dialect == "sqlite":
        # event_log already has SQLite triggers from bb8ea93e5a0f.
        _sqlite_triggers("ops_activity")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        _drop_pg("ops_activity")
        _drop_pg("event_log")
    elif dialect == "sqlite":
        _drop_sqlite("ops_activity")
