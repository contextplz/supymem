"""Minimal opt-in inbox sidecars, without rewriting history."""
from alembic import op
import sqlalchemy as sa

revision = "s057inboxbrief"
down_revision = "s056multigmail"
branch_labels = None
depends_on = None


def upgrade():
    from app.inbox.store import TABLES
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    from app.inbox.store import TABLES
    connection = op.get_bind()
    existing = set(sa.inspect(connection).get_table_names())
    tables = [table for table in TABLES if table.name in existing]
    if connection.dialect.name == "postgresql" and tables:
        connection.execute(sa.text("LOCK TABLE " + ", ".join(table.name for table in tables)
                                   + " IN ACCESS EXCLUSIVE MODE"))
    if any(connection.execute(sa.select(1).select_from(table).limit(1)).first() for table in tables):
        raise RuntimeError("inbox_briefing_history_prevents_downgrade")
    for table in reversed(tables):
        table.drop(connection, checkfirst=True)
