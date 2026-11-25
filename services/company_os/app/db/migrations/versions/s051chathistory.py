"""Conversation pins, archives, and folders."""
import sqlalchemy as sa
from alembic import op

revision = "s051chathistory"
down_revision = "s050gtmlearning"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chat_conversations", table_args=(
        sa.CheckConstraint("visibility IN ('private', 'legacy_shared')"),
        sa.CheckConstraint("visibility != 'private' OR creator_person_id IS NOT NULL"),
    )) as batch:
        batch.add_column(sa.Column("pinned_at", sa.DateTime()))
        batch.add_column(sa.Column("archived_at", sa.DateTime()))
        batch.add_column(sa.Column("folder", sa.Text()))
        batch.create_check_constraint("ck_chat_conversation_folder_length", "length(folder) <= 60")


def downgrade():
    with op.batch_alter_table("chat_conversations", table_args=(
        sa.CheckConstraint("visibility IN ('private', 'legacy_shared')"),
        sa.CheckConstraint("visibility != 'private' OR creator_person_id IS NOT NULL"),
    )) as batch:
        batch.drop_constraint("ck_chat_conversation_folder_length", type_="check")
        for name in ("folder", "archived_at", "pinned_at"):
            batch.drop_column(name)
