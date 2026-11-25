"""Add founder-led outreach columns, isolation workspace_id, and policy tables.

Revision ID: q1l2m3n4o5p6
Revises: p0k1l2m3n4o5
Create Date: 2026-09-10

SQLite downgrade leaves additive columns in place (same pattern as n8i9j0k1l2m3).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q1l2m3n4o5p6"
down_revision: str | None = "p0k1l2m3n4o5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_cols(insp: sa.Inspector, table: str) -> set[str]:
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _add_columns(table: str, existing: set[str], columns: list[tuple[str, sa.Column]]) -> None:
    for name, col in columns:
        if name not in existing:
            op.add_column(table, col)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    _add_columns(
        "gtm_companies",
        _existing_cols(insp, "gtm_companies"),
        [
            ("company_type", sa.Column("company_type", sa.Text())),
            ("status_quo_stack_json", sa.Column("status_quo_stack_json", sa.Text(), server_default="[]")),
            ("wedge_fit", sa.Column("wedge_fit", sa.Text())),
            ("notes", sa.Column("notes", sa.Text())),
        ],
    )
    _add_columns(
        "gtm_contacts",
        _existing_cols(insp, "gtm_contacts"),
        [
            ("ask_rung", sa.Column("ask_rung", sa.Text(), server_default="none")),
            ("ask_rung_changed_at", sa.Column("ask_rung_changed_at", sa.DateTime())),
            ("score_void", sa.Column("score_void", sa.Integer(), server_default="0")),
            ("score_sum", sa.Column("score_sum", sa.Integer())),
            ("score_band", sa.Column("score_band", sa.Text())),
            ("score_factors_json", sa.Column("score_factors_json", sa.Text(), server_default="{}")),
            ("score_explain", sa.Column("score_explain", sa.Text())),
            ("why_this_week", sa.Column("why_this_week", sa.Text())),
            ("why_this_week_confirmed_at", sa.Column("why_this_week_confirmed_at", sa.DateTime())),
            ("score_override", sa.Column("score_override", sa.Text())),
            ("score_override_reason", sa.Column("score_override_reason", sa.Text())),
            ("exact_language_json", sa.Column("exact_language_json", sa.Text(), server_default="[]")),
            ("recipient_geo", sa.Column("recipient_geo", sa.Text())),
            ("contact_provenance", sa.Column("contact_provenance", sa.Text())),
            ("mutual_count", sa.Column("mutual_count", sa.Integer())),
            ("buyer_tag", sa.Column("buyer_tag", sa.Text())),
            ("recipient_seniority", sa.Column("recipient_seniority", sa.Text())),
            ("layer", sa.Column("layer", sa.Text(), server_default="B")),
            ("personalize_mode", sa.Column("personalize_mode", sa.Text())),
            ("sku_key", sa.Column("sku_key", sa.Text())),
            ("drop_reason", sa.Column("drop_reason", sa.Text())),
            ("cadence_week", sa.Column("cadence_week", sa.Integer())),
            ("next_action_at", sa.Column("next_action_at", sa.DateTime())),
            ("next_action_kind", sa.Column("next_action_kind", sa.Text())),
            ("i_actually_know_them", sa.Column("i_actually_know_them", sa.Integer(), server_default="0")),
        ],
    )
    _add_columns(
        "gtm_signals",
        _existing_cols(insp, "gtm_signals"),
        [
            ("contact_id", sa.Column("contact_id", sa.Text())),
            ("source", sa.Column("source", sa.Text())),
            ("trigger_code", sa.Column("trigger_code", sa.Text())),
            ("raw_text", sa.Column("raw_text", sa.Text())),
            ("confidence", sa.Column("confidence", sa.Text())),
            ("layer", sa.Column("layer", sa.Text(), server_default="A")),
            ("extracted_quote", sa.Column("extracted_quote", sa.Text())),
            ("timing_observed_at", sa.Column("timing_observed_at", sa.DateTime())),
        ],
    )
    _add_columns(
        "gtm_touches",
        _existing_cols(insp, "gtm_touches"),
        [
            ("workspace_id", sa.Column("workspace_id", sa.Text())),
            ("ask_rung", sa.Column("ask_rung", sa.Text())),
            ("outcome", sa.Column("outcome", sa.Text())),
            ("send_mode", sa.Column("send_mode", sa.Text())),
            ("honesty_json", sa.Column("honesty_json", sa.Text())),
            ("sku_key", sa.Column("sku_key", sa.Text())),
            ("copied_at", sa.Column("copied_at", sa.DateTime())),
            ("logged_by", sa.Column("logged_by", sa.Text())),
            ("numerator_event", sa.Column("numerator_event", sa.Text())),
        ],
    )
    _add_columns(
        "gtm_enrollments",
        _existing_cols(insp, "gtm_enrollments"),
        [("workspace_id", sa.Column("workspace_id", sa.Text()))],
    )
    _add_columns(
        "gtm_email_events",
        _existing_cols(insp, "gtm_email_events"),
        [("workspace_id", sa.Column("workspace_id", sa.Text()))],
    )
    _add_columns(
        "gtm_email_send_queue",
        _existing_cols(insp, "gtm_email_send_queue"),
        [("workspace_id", sa.Column("workspace_id", sa.Text()))],
    )
    _add_columns(
        "gtm_research_snapshots",
        _existing_cols(insp, "gtm_research_snapshots"),
        [("workspace_id", sa.Column("workspace_id", sa.Text()))],
    )
    _add_columns(
        "gtm_linkedin_queue",
        _existing_cols(insp, "gtm_linkedin_queue"),
        [
            ("copied_at", sa.Column("copied_at", sa.DateTime())),
            ("sent_at", sa.Column("sent_at", sa.DateTime())),
            ("honesty_json", sa.Column("honesty_json", sa.Text())),
            ("send_mode", sa.Column("send_mode", sa.Text())),
        ],
    )
    _add_columns(
        "gtm_engine_state",
        _existing_cols(insp, "gtm_engine_state"),
        [
            ("outreach_week", sa.Column("outreach_week", sa.Integer(), server_default="1")),
            (
                "outbound_mode",
                sa.Column("outbound_mode", sa.Text(), server_default="email_department"),
            ),
        ],
    )

    if "gtm_contacts" in insp.get_table_names():
        op.execute(
            """
            UPDATE gtm_touches
            SET workspace_id = (
                SELECT gtm_contacts.workspace_id FROM gtm_contacts
                WHERE gtm_contacts.id = gtm_touches.contact_id
            )
            WHERE workspace_id IS NULL
            """
        )
        op.execute(
            """
            UPDATE gtm_enrollments
            SET workspace_id = (
                SELECT gtm_contacts.workspace_id FROM gtm_contacts
                WHERE gtm_contacts.id = gtm_enrollments.contact_id
            )
            WHERE workspace_id IS NULL
            """
        )
        op.execute(
            """
            UPDATE gtm_email_events
            SET workspace_id = (
                SELECT gtm_contacts.workspace_id FROM gtm_contacts
                WHERE gtm_contacts.id = gtm_email_events.contact_id
            )
            WHERE workspace_id IS NULL AND contact_id IS NOT NULL
            """
        )
        op.execute(
            """
            UPDATE gtm_email_send_queue
            SET workspace_id = (
                SELECT gtm_contacts.workspace_id FROM gtm_contacts
                WHERE gtm_contacts.id = gtm_email_send_queue.contact_id
            )
            WHERE workspace_id IS NULL
            """
        )
        op.execute(
            """
            UPDATE gtm_research_snapshots
            SET workspace_id = (
                SELECT gtm_contacts.workspace_id FROM gtm_contacts
                WHERE gtm_contacts.id = gtm_research_snapshots.contact_id
            )
            WHERE workspace_id IS NULL
            """
        )

    tables = set(insp.get_table_names())
    if "gtm_outreach_settings" not in tables:
        op.create_table(
            "gtm_outreach_settings",
            sa.Column("workspace_id", sa.Text(), primary_key=True),
            sa.Column("onboarding_status", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("outbound_mode", sa.Text(), nullable=False, server_default="human_clipboard"),
            sa.Column("sender_geo", sa.Text()),
            sa.Column("sender_postal_address", sa.Text()),
            sa.Column("composio_user_id", sa.Text()),
            sa.Column("aup_accepted_at", sa.DateTime()),
            sa.Column("max_connect_per_week", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("max_dm_per_week", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("max_email_per_week", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("send_enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "gtm_suppressions" not in tables:
        op.create_table(
            "gtm_suppressions",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("email", sa.Text()),
            sa.Column("linkedin_url", sa.Text()),
            sa.Column("reason", sa.Text()),
            sa.Column("source", sa.Text(), nullable=False, server_default="manual"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_gtm_suppressions_email", "gtm_suppressions", ["workspace_id", "email"])
        op.create_index("ix_gtm_suppressions_li", "gtm_suppressions", ["workspace_id", "linkedin_url"])
        op.create_index("ix_gtm_suppressions_workspace_id", "gtm_suppressions", ["workspace_id"])
    if "gtm_send_policies" not in tables:
        op.create_table(
            "gtm_send_policies",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text()),
            sa.Column("geo", sa.Text(), nullable=False),
            sa.Column("channel", sa.Text(), nullable=False),
            sa.Column("rule_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_gtm_send_policies_geo",
            "gtm_send_policies",
            ["workspace_id", "geo", "channel"],
        )
    if "gtm_artifact_skus" not in tables:
        op.create_table(
            "gtm_artifact_skus",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("sku_key", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("period", sa.Text()),
            sa.Column("title", sa.Text()),
            sa.Column("body_md", sa.Text()),
            sa.Column("match_tags_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("approved_at", sa.DateTime()),
            sa.UniqueConstraint("workspace_id", "sku_key", name="uq_gtm_sku_key"),
        )
        op.create_index("ix_gtm_artifact_skus_workspace_id", "gtm_artifact_skus", ["workspace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "gtm_artifact_skus" in tables:
        op.drop_table("gtm_artifact_skus")
    if "gtm_send_policies" in tables:
        op.drop_table("gtm_send_policies")
    if "gtm_suppressions" in tables:
        op.drop_table("gtm_suppressions")
    if "gtm_outreach_settings" in tables:
        op.drop_table("gtm_outreach_settings")
    if dialect == "sqlite":
        # Additive columns remain; create_all / ensure_supymem_schema is additive-only.
        return
