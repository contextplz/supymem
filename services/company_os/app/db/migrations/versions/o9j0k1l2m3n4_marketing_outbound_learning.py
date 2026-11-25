"""Expand marketing job kinds: outbound + enable_ad + learning events.

Revision ID: o9j0k1l2m3n4
Revises: n8i9j0k1l2m3
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o9j0k1l2m3n4"
down_revision: str | None = "n8i9j0k1l2m3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CHECK = (
    "kind IN ("
    "'create_content', 'create_visual', 'publish_content', "
    "'find_threads', 'draft_reply', 'publish_reply', "
    "'research_topic', 'check_result', "
    "'generate_image', 'generate_video', 'operate_app', "
    "'operate_browser', 'create_ad', 'enable_ad', 'ingest_asset', "
    "'enqueue_outreach', 'send_email_touch'"
    ")"
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)
    if dialect == "sqlite":
        # Table rebuild for CHECK lives in ensure_marketing_media_schema.
        pass
    else:
        op.drop_constraint("ck_marketing_job_kind", "marketing_jobs", type_="check")
        op.create_check_constraint("ck_marketing_job_kind", "marketing_jobs", _KIND_CHECK)

    if "marketing_learning_events" not in insp.get_table_names():
        op.create_table(
            "marketing_learning_events",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("job_id", sa.Text()),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("platform", sa.Text()),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_marketing_learning_events_workspace_id",
            "marketing_learning_events",
            ["workspace_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "marketing_learning_events" in insp.get_table_names():
        op.drop_index(
            "ix_marketing_learning_events_workspace_id",
            table_name="marketing_learning_events",
        )
        op.drop_table("marketing_learning_events")
