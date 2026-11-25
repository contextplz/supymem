"""Track receipt of the current device token without storing plaintext."""
import sqlalchemy as sa
from alembic import op

revision = "s049refreshuse"
down_revision = "s048gatewayidentities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("desktop_devices")}
    if "refresh_token_used" not in columns:
        op.add_column("desktop_devices", sa.Column("refresh_token_used", sa.Boolean(),
                                                 nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("desktop_devices", "refresh_token_used")
