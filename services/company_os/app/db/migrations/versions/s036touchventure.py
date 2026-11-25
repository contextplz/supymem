"""Attribute future actual outreach events; historical NULL stays unqualified."""
import sqlalchemy as sa
from alembic import op

revision = "s036touchventure"
down_revision = "s035siteventure"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gtm_touches", sa.Column("venture_id", sa.Text(), nullable=True))
    op.add_column("gtm_touches", sa.Column("provider_thread_id", sa.Text(), nullable=True))
    op.create_index("ix_gtm_touches_venture_id", "gtm_touches", ["venture_id"])
    op.create_index("uq_gtm_touch_venture_reply", "gtm_touches", ["workspace_id", "venture_id", "contact_id", "message_id"], unique=True,
                    postgresql_where=sa.text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL"),
                    sqlite_where=sa.text("direction = 'in' AND venture_id IS NOT NULL AND message_id IS NOT NULL"))


def downgrade():
    op.drop_index("uq_gtm_touch_venture_reply", table_name="gtm_touches")
    op.drop_index("ix_gtm_touches_venture_id", table_name="gtm_touches")
    op.drop_column("gtm_touches", "provider_thread_id")
    op.drop_column("gtm_touches", "venture_id")
