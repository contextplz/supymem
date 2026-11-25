"""Per-founder consent for Charlie to use the founder's own coding agents. Additive."""
from alembic import op
import sqlalchemy as sa

revision = "s061codingconsent"
down_revision = "s059scaleidx"
branch_labels = None
depends_on = None


def upgrade():
    from app.coding_agents_consent import TABLES
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    from app.coding_agents_consent import TABLES
    connection = op.get_bind()
    existing = set(sa.inspect(connection).get_table_names())
    tables = [table for table in TABLES if table.name in existing]
    if connection.dialect.name == "postgresql" and tables:
        connection.execute(sa.text("LOCK TABLE " + ", ".join(table.name for table in tables)
                                   + " IN ACCESS EXCLUSIVE MODE"))
    if any(connection.execute(sa.select(1).select_from(table).limit(1)).first() for table in tables):
        raise RuntimeError("coding_agent_consent_history_prevents_downgrade")
    for table in reversed(tables):
        table.drop(connection, checkfirst=True)
