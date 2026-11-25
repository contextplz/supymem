"""Durable founder tasks, device checkpoints, scheduler and estimated deadlines."""
import sqlalchemy as sa
from alembic import op

revision = "s045tasks"
down_revision = "s044grantthreshold"
branch_labels = None
depends_on = None


def upgrade():
    from app.db.task_models import MaintenanceSchedule, Task, TaskEvent, TaskRun, TaskStep
    bind = op.get_bind()
    for table in (Task.__table__, TaskRun.__table__, TaskStep.__table__, TaskEvent.__table__, MaintenanceSchedule.__table__):
        table.create(bind, checkfirst=True)  # s001 copies live ORM metadata on a fresh install.
    for table, column in (
        ("people", sa.Column("timezone", sa.Text(), nullable=True)),
        ("jobs", sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("plan_tasks", sa.Column("due_at", sa.DateTime(), nullable=True)),
    ):
        if column.name not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(table, column)
    if bind.dialect.name == "postgresql":
        op.execute("""CREATE OR REPLACE FUNCTION task_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'task_events_append_only'; END; $$""")
        op.execute("DROP TRIGGER IF EXISTS task_events_append_only ON task_events")
        op.execute("CREATE TRIGGER task_events_append_only BEFORE UPDATE OR DELETE ON task_events FOR EACH ROW EXECUTE FUNCTION task_events_append_only()")


def downgrade():
    bind = op.get_bind()
    tables = ("maintenance_schedules", "task_events", "task_steps", "task_runs", "tasks")
    existing = set(sa.inspect(bind).get_table_names())
    present = [name for name in tables if name in existing]
    if bind.dialect.name == "postgresql" and present:
        # Keep the empty proof and DROP atomic against a concurrent insertion.
        bind.execute(sa.text("LOCK TABLE " + ", ".join(present) + " IN ACCESS EXCLUSIVE MODE"))
    if any(bind.execute(sa.text("SELECT 1 FROM " + name + " LIMIT 1")).first() for name in present):
        raise RuntimeError("task_history_prevents_downgrade")
    for name in present:
        op.drop_table(name)
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS task_events_append_only()")
    # Additive fields on existing tables stay available to rollback code.
