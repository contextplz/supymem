"""Add trusted in-app destinations to durable desktop notices."""

import sqlalchemy as sa
from alembic import op

revision = "s021noticetargets"
down_revision = "s020appruntime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("notices")}
    # s001 creates its table set from current metadata on a fresh database.
    if "target_path" not in columns:
        op.add_column("notices", sa.Column("target_path", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("notices")}
    if "target_path" in columns:
        op.drop_column("notices", "target_path")
