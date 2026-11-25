"""Marketing media assets + expanded job kinds.

Revision ID: n8i9j0k1l2m3
Revises: m7g8h9i0j1k2
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n8i9j0k1l2m3"
down_revision: str | None = "m7g8h9i0j1k2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CHECK = (
    "kind IN ("
    "'create_content', 'create_visual', 'publish_content', "
    "'find_threads', 'draft_reply', 'publish_reply', "
    "'research_topic', 'check_result', "
    "'generate_image', 'generate_video', 'operate_app', "
    "'operate_browser', 'create_ad', 'ingest_asset'"
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

    if "marketing_assets" not in insp.get_table_names():
        op.create_table(
            "marketing_assets",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False, server_default="upload"),
            sa.Column("title", sa.Text()),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("mime", sa.Text(), nullable=False, server_default="image/png"),
            sa.Column("width", sa.Integer()),
            sa.Column("height", sa.Integer()),
            sa.Column("duration_ms", sa.Integer()),
            sa.Column("license_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("review_status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "kind IN ('logo', 'founder', 'product', 'generated_image', "
                "'generated_video', 'stock', 'other')",
                name="ck_marketing_asset_kind",
            ),
            sa.CheckConstraint(
                "review_status IN ('pending', 'approved', 'rejected')",
                name="ck_marketing_asset_review",
            ),
        )
        op.create_index(
            "ix_marketing_assets_workspace",
            "marketing_assets",
            ["workspace_id", "kind", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "marketing_assets" in insp.get_table_names():
        op.drop_index("ix_marketing_assets_workspace", table_name="marketing_assets")
        op.drop_table("marketing_assets")
