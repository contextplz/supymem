"""Content engine: publishing accounts, OAuth state, briefs, topics, posts, metrics.

Revision ID: l6f7g8h9i0j1
Revises: k5e6f7g8h9i0
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l6f7g8h9i0j1"
down_revision: str | None = "k5e6f7g8h9i0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_accounts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_account", sa.Text(), nullable=False, server_default=""),
        sa.Column("handle", sa.Text()),
        sa.Column("author_urn", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("access_secret_ref", sa.Text()),
        sa.Column("refresh_secret_ref", sa.Text()),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("extra_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("posts_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_key", sa.Text()),
        sa.Column("last_post_at", sa.DateTime()),
        sa.Column("health", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("paused_reason", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "platform",
            "external_account",
            name="uq_content_account_identity",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'expired', 'revoked', 'error')",
            name="ck_content_account_status",
        ),
    )
    op.create_index(
        "ix_content_accounts_workspace_id", "content_accounts", ["workspace_id"]
    )
    op.create_index(
        "ix_content_accounts_platform",
        "content_accounts",
        ["workspace_id", "platform", "status"],
    )

    op.create_table(
        "content_oauth_states",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text()),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text()),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_content_oauth_states_workspace_id",
        "content_oauth_states",
        ["workspace_id"],
    )

    op.create_table(
        "content_briefs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("source_name", sa.Text()),
        sa.Column("product_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("icp_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("competitors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("voice_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("claims_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("cadence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "version", name="uq_content_brief_version"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_content_brief_status"
        ),
    )
    op.create_index(
        "ix_content_briefs_workspace_id", "content_briefs", ["workspace_id"]
    )
    op.create_index(
        "ix_content_briefs_status", "content_briefs", ["workspace_id", "status"]
    )

    op.create_table(
        "content_topics",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column(
            "brief_id", sa.Text(), sa.ForeignKey("content_briefs.id"), nullable=False
        ),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("icp_segment", sa.Text()),
        sa.Column("intent", sa.Text(), nullable=False, server_default="value"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'drafted', 'published', 'spent', 'dead')",
            name="ck_content_topic_status",
        ),
        sa.CheckConstraint(
            "intent IN ('value', 'promo')", name="ck_content_topic_intent"
        ),
    )
    op.create_index(
        "ix_content_topics_workspace_id", "content_topics", ["workspace_id"]
    )
    op.create_index(
        "ix_content_topics_status",
        "content_topics",
        ["workspace_id", "status", "score"],
    )

    op.create_table(
        "content_posts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column(
            "topic_id", sa.Text(), sa.ForeignKey("content_topics.id"), nullable=False
        ),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("content_accounts.id")),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False, server_default="value"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.Text()),
        sa.Column("target", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("simhash", sa.Text()),
        sa.Column("lint_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("critic_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("refusal_reason", sa.Text()),
        sa.Column("scheduled_for", sa.DateTime()),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("external_id", sa.Text()),
        sa.Column("external_url", sa.Text()),
        sa.Column(
            "dry_run", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('draft', 'refused', 'approved', 'scheduled', "
            "'published', 'failed', 'removed')",
            name="ck_content_post_state",
        ),
    )
    op.create_index(
        "ix_content_posts_workspace_id", "content_posts", ["workspace_id"]
    )
    op.create_index(
        "ix_content_posts_state",
        "content_posts",
        ["workspace_id", "state", "scheduled_for"],
    )
    op.create_index(
        "ix_content_posts_platform",
        "content_posts",
        ["workspace_id", "platform", "published_at"],
    )

    op.create_table(
        "content_metrics",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column(
            "post_id", sa.Text(), sa.ForeignKey("content_posts.id"), nullable=False
        ),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reactions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reposts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "visible", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_content_metrics_workspace_id", "content_metrics", ["workspace_id"]
    )
    op.create_index(
        "ix_content_metrics_post", "content_metrics", ["post_id", "captured_at"]
    )

    op.create_table(
        "content_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text()),
        sa.Column("post_id", sa.Text()),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_content_events_workspace_id", "content_events", ["workspace_id"]
    )
    op.create_index(
        "ix_content_events_kind",
        "content_events",
        ["workspace_id", "kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("content_events")
    op.drop_table("content_metrics")
    op.drop_table("content_posts")
    op.drop_table("content_topics")
    op.drop_table("content_briefs")
    op.drop_table("content_oauth_states")
    op.drop_table("content_accounts")
