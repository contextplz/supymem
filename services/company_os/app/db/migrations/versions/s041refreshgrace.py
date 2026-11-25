"""Fixed desktop refresh retry window; retain only token digests."""
import sqlalchemy as sa
from alembic import op

revision = "s041refreshgrace"
down_revision = "s040errorreports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # s001 historically copies live ORM metadata on a fresh install.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("desktop_devices")}
    if "previous_refresh_token_hash" not in columns:
        op.add_column("desktop_devices", sa.Column("previous_refresh_token_hash", sa.Text(), nullable=True))
    if "refresh_rotated_at" not in columns:
        op.add_column("desktop_devices", sa.Column("refresh_rotated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("desktop_devices", "refresh_rotated_at")
    op.drop_column("desktop_devices", "previous_refresh_token_hash")
