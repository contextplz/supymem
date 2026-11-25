"""Bind fresh site drafts to their owning venture without attributing legacy rows."""
import sqlalchemy as sa
from alembic import op

revision = "s035siteventure"
down_revision = "s034referrals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_versions", sa.Column("venture_id", sa.Text(), nullable=True))
    op.create_index("ix_site_versions_venture", "site_versions", ["workspace_id", "venture_id", "version"])


def downgrade():
    op.drop_index("ix_site_versions_venture", table_name="site_versions")
    op.drop_column("site_versions", "venture_id")
