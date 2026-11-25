"""Keep anonymous campaign attribution with the consented waitlist entry."""
import sqlalchemy as sa
from alembic import op

revision = "s053waitlistprops"
down_revision = "s052referralrewards"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("venture_waitlist_entries", sa.Column("props", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("venture_waitlist_entries", "props")
