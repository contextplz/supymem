"""Add Polsia ops supersets tables (doc 49).

Revision ID: d8e9f0a1b2c3
Revises: c7d1e2f3a4b5
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_company_config",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mission", sa.Text()),
        sa.Column("vision", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("target_market", sa.Text()),
        sa.Column("value_prop", sa.Text()),
        sa.Column("pricing_model_json", sa.Text()),
        sa.Column("goals_json", sa.Text()),
        sa.Column("kpis_json", sa.Text()),
        sa.Column("website_url", sa.Text()),
        sa.Column("github_repo", sa.Text()),
        sa.Column("product_type", sa.Text()),
        sa.Column("industry", sa.Text()),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("daily_cycle_hour", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id"),
    )
    op.create_index("ix_ops_company_config_workspace_id", "ops_company_config", ["workspace_id"])

    op.create_table(
        "ops_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("source", sa.Text(), nullable=False, server_default="orchestrator"),
        sa.Column("scheduled_date", sa.DateTime()),
        sa.Column("result_summary", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("metadata_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_tasks_workspace_id", "ops_tasks", ["workspace_id"])
    op.create_index("ix_ops_tasks_status", "ops_tasks", ["workspace_id", "status"])
    op.create_index("ix_ops_tasks_agent_type", "ops_tasks", ["workspace_id", "agent_type"])

    op.create_table(
        "ops_agent_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("ops_tasks.id")),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False, server_default="task"),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("input_context_json", sa.Text()),
        sa.Column("output_json", sa.Text()),
        sa.Column("raw_log", sa.Text()),
        sa.Column("tokens_used", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("duration_secs", sa.Float()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
    )
    op.create_index("ix_ops_agent_runs_workspace_id", "ops_agent_runs", ["workspace_id"])

    op.create_table(
        "ops_activity",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.Text()),
        sa.Column("level", sa.Text(), nullable=False, server_default="info"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_activity_workspace_id", "ops_activity", ["workspace_id"])
    op.create_index("ix_ops_activity_created_at", "ops_activity", ["workspace_id", "created_at"])
    op.create_index("ix_ops_activity_agent_type", "ops_activity", ["workspace_id", "agent_type"])

    op.create_table(
        "ops_memory",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.Text()),
        sa.Column("tags_json", sa.Text()),
        sa.Column("chroma_id", sa.Text(), unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_memory_workspace_id", "ops_memory", ["workspace_id"])
    op.create_index("ix_ops_memory_category", "ops_memory", ["workspace_id", "category"])

    op.create_table(
        "ops_social_posts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="twitter"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("tweet_id", sa.Text()),
        sa.Column("scheduled_for", sa.DateTime()),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("engagement_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_social_posts_workspace_id", "ops_social_posts", ["workspace_id"])

    op.create_table(
        "ops_social_engagements",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("mention_id", sa.Text()),
        sa.Column("author_handle", sa.Text()),
        sa.Column("content", sa.Text()),
        sa.Column("our_reply", sa.Text()),
        sa.Column("reply_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "mention_id", name="uq_ops_social_engagement_mention"),
    )
    op.create_index(
        "ix_ops_social_engagements_workspace_id", "ops_social_engagements", ["workspace_id"]
    )

    op.create_table(
        "ops_prospects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("company", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("source", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "email", name="uq_ops_prospect_email"),
    )
    op.create_index("ix_ops_prospects_workspace_id", "ops_prospects", ["workspace_id"])

    op.create_table(
        "ops_email_campaigns",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text()),
        sa.Column("target_segment", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("total_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_opened", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_replied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_email_campaigns_workspace_id", "ops_email_campaigns", ["workspace_id"])

    op.create_table(
        "ops_email_logs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("prospect_id", sa.Text(), sa.ForeignKey("ops_prospects.id"), nullable=False),
        sa.Column("campaign_id", sa.Text(), sa.ForeignKey("ops_email_campaigns.id")),
        sa.Column("sequence_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("subject", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("sendgrid_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="sent"),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("opened_at", sa.DateTime()),
        sa.Column("replied_at", sa.DateTime()),
        sa.Column("follow_up_due", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_email_logs_workspace_id", "ops_email_logs", ["workspace_id"])

    op.create_table(
        "ops_ad_campaigns",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("daily_budget_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_spent_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_ad_campaigns_workspace_id", "ops_ad_campaigns", ["workspace_id"])

    op.create_table(
        "ops_ad_metrics",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("campaign_id", sa.Text(), sa.ForeignKey("ops_ad_campaigns.id"), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spend_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ctr", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cpc", sa.Float(), nullable=False, server_default="0"),
        sa.Column("roas", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_ops_ad_metrics_workspace_id", "ops_ad_metrics", ["workspace_id"])

    op.create_table(
        "ops_competitors",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("website", sa.Text()),
        sa.Column("pricing_info_json", sa.Text()),
        sa.Column("positioning", sa.Text()),
        sa.Column("strengths_json", sa.Text()),
        sa.Column("weaknesses_json", sa.Text()),
        sa.Column("last_researched", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_competitors_workspace_id", "ops_competitors", ["workspace_id"])

    op.create_table(
        "ops_stripe_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("stripe_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text()),
        sa.Column("amount_cents", sa.Integer()),
        sa.Column("currency", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="processed"),
        sa.Column("raw_payload_json", sa.Text()),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "stripe_event_id", name="uq_ops_stripe_event"),
    )
    op.create_index("ix_ops_stripe_events_workspace_id", "ops_stripe_events", ["workspace_id"])
    op.create_index(
        "ix_ops_stripe_events_event_type", "ops_stripe_events", ["workspace_id", "event_type"]
    )

    op.create_table(
        "ops_revenue_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("mrr_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("arr_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_subscribers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("churned_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_revenue_month_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stripe_balance_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "snapshot_date", name="uq_ops_revenue_snapshot"),
    )
    op.create_index(
        "ix_ops_revenue_snapshots_workspace_id", "ops_revenue_snapshots", ["workspace_id"]
    )
    op.create_index(
        "ix_ops_revenue_snapshots_date", "ops_revenue_snapshots", ["workspace_id", "snapshot_date"]
    )

    op.create_table(
        "ops_expense_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="usd"),
        sa.Column("description", sa.Text()),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("external_ref", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ops_expense_records_workspace_id", "ops_expense_records", ["workspace_id"])

    op.create_table(
        "ops_daily_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("morning_plan", sa.Text()),
        sa.Column("evening_summary", sa.Text()),
        sa.Column("tasks_planned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics_snapshot_json", sa.Text()),
        sa.Column("insights_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "report_date", name="uq_ops_daily_report"),
    )
    op.create_index("ix_ops_daily_reports_workspace_id", "ops_daily_reports", ["workspace_id"])

    op.create_table(
        "ops_deployments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("trigger_source", sa.Text(), nullable=False, server_default="api"),
        sa.Column("external_id", sa.Text()),
        sa.Column("commit_sha", sa.Text()),
        sa.Column("health_check_url", sa.Text()),
        sa.Column("health_check_passed", sa.Integer()),
        sa.Column("summary", sa.Text()),
        sa.Column("metadata_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
    )
    op.create_index("ix_ops_deployments_workspace_id", "ops_deployments", ["workspace_id"])


def downgrade() -> None:
    for table in [
        "ops_deployments",
        "ops_daily_reports",
        "ops_expense_records",
        "ops_revenue_snapshots",
        "ops_stripe_events",
        "ops_competitors",
        "ops_ad_metrics",
        "ops_ad_campaigns",
        "ops_email_logs",
        "ops_email_campaigns",
        "ops_prospects",
        "ops_social_engagements",
        "ops_social_posts",
        "ops_memory",
        "ops_activity",
        "ops_agent_runs",
        "ops_tasks",
        "ops_company_config",
    ]:
        op.drop_table(table)
