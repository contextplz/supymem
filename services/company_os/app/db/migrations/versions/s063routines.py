"""Reuse task schedules and add an index for founder-owned routine lists."""
from alembic import op
from sqlalchemy import text

revision = "s063routines"
down_revision = "s062spend"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_tasks_owner_schedule", "tasks", ["workspace_id", "venture_id", "owner_person_id", "kind"], if_not_exists=True)


def downgrade():
    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM tasks WHERE "
            "create_key LIKE 'routine:%' OR create_key LIKE 'routine-run:%')")):
        raise RuntimeError("routine_history_prevents_downgrade")
    op.drop_index("ix_tasks_owner_schedule", table_name="tasks", if_exists=True)
