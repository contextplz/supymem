"""Durable error fingerprints and founder problem reports."""
import sqlalchemy as sa
from alembic import op

revision = "s040errorreports"
down_revision = "s039godmoderequests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tz = sa.DateTime(timezone=True)
    op.create_table("error_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
        sa.Column("source", sa.Text(), nullable=False), sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("place", sa.Text(), nullable=False, server_default=""),
        sa.Column("frame", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.Text(), nullable=False, server_default="error"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen", tz, nullable=False), sa.Column("last_seen", tz, nullable=False),
        sa.Column("release", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("window_start", tz, nullable=False),
        sa.Column("window_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("alerted_at", tz),
        sa.Column("sample_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("affected_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("resolved_at", tz), sa.Column("resolved_by", sa.Text()))
    op.create_index("ix_error_reports_status", "error_reports", ["status"])
    op.create_index("ix_error_reports_last_seen", "error_reports", ["last_seen"])
    op.create_table("problem_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text()), sa.Column("venture_id", sa.Text()),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("page", sa.Text(), nullable=False, server_default=""),
        sa.Column("app_version", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("fingerprints_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("screenshot", sa.LargeBinary()), sa.Column("screenshot_type", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at", tz, nullable=False))
    op.create_index("ix_problem_reports_person_id", "problem_reports", ["person_id"])
    op.create_index("ix_problem_reports_created_at", "problem_reports", ["created_at"])


def downgrade() -> None:
    op.drop_table("problem_reports")
    op.drop_table("error_reports")
