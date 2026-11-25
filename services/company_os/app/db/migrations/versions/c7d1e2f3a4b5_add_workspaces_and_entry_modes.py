"""Phase 10: workspaces, entry modes, and every table docs 35-47 introduce.

Additive only. Nothing is dropped. The backfill in :func:`_backfill` is
asserted inside the migration (46 T10.2) because a silent row loss here would
surface weeks later as missing history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d1e2f3a4b5"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LOCAL_PERSON_ID = "per_local000000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _append_only_triggers(table: str) -> None:
    """Block UPDATE/DELETE on an append-only table."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table} BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END;
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table} BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END;
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION '{table} is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {table}_append_only();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {table}_append_only();
            """
        )


def _drop_append_only_triggers(table: str) -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    for col in columns:
        op.add_column(table, col)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    _create_foundation()
    _create_feature_tables()
    _extend_existing()
    _backfill()


def _create_foundation() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entry_mode", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("tenancy", sa.Text(), nullable=False, server_default="local"),
        sa.Column("autonomy_level", sa.Text(), nullable=False, server_default="L1"),
        sa.Column("observe_level", sa.Text(), nullable=False, server_default="O0"),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime()),
    )
    op.create_index("ix_workspaces_owner", "workspaces", ["owner_id", "created_at"])
    # Exactly one desk per person (35 §4).
    op.execute(
        """
        CREATE UNIQUE INDEX ux_one_desk_per_person
        ON workspaces (owner_id) WHERE kind = 'desk'
        """
    )

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("person_id", sa.Text(), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("can_approve", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "person_id"),
    )

    op.create_table(
        "invites",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("invited_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime()),
        sa.UniqueConstraint("workspace_id", "email"),
    )


def _create_feature_tables() -> None:
    # --- 43 boundary ledger (append-only) --------------------------------
    op.create_table(
        "boundary_ledger",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("classes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("bytes_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("redactions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("block_reason", sa.Text()),
        sa.Column("no_train_asserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trace_id", sa.Text()),
    )
    op.create_index("ix_boundary_ledger_ws", "boundary_ledger", ["workspace_id", "at"])
    _append_only_triggers("boundary_ledger")

    # --- 36 engagements ---------------------------------------------------
    op.create_table(
        "engagements",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("size", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("request_verbatim", sa.Text(), nullable=False),
        sa.Column("definition_of_done", sa.Text(), nullable=False),
        sa.Column("not_doing_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("target_ref", sa.Text()),
        sa.Column("deliverables_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("plan_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("revert_hint", sa.Text()),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_cap_usd", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("origin", sa.Text(), nullable=False, server_default="composer"),
        sa.Column("origin_ref", sa.Text()),
        sa.Column("parent_workflow_id", sa.Text()),
        sa.Column("fingerprint", sa.Text()),
        sa.Column("clarification_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime()),
    )
    op.create_index("ix_engagements_ws", "engagements", ["workspace_id", "created_at"])
    op.create_index("ix_engagements_status", "engagements", ["workspace_id", "status"])

    # --- 37 idea lab ------------------------------------------------------
    op.create_table(
        "opportunity_slates",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("seed_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("seed_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_slates_ws", "opportunity_slates", ["workspace_id", "created_at"])

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "slate_id", sa.Text(), sa.ForeignKey("opportunity_slates.id"), nullable=False
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("one_liner", sa.Text(), nullable=False),
        sa.Column("first_customer", sa.Text(), nullable=False),
        sa.Column("wedge", sa.Text(), nullable=False),
        sa.Column("why_now", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("kill_criteria", sa.Text(), nullable=False),
        sa.Column("shape_tags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("effort_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("researched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("reject_reason", sa.Text()),
        sa.Column("teaser_path", sa.Text()),
        sa.Column("promoted_venture_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_opps_ws", "opportunities", ["workspace_id", "status", "created_at"])

    # --- 45 first look ----------------------------------------------------
    op.create_table(
        "site_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("archetype", sa.Text(), nullable=False),
        sa.Column("brandkit_hash", sa.Text(), nullable=False),
        sa.Column("copy_hash", sa.Text(), nullable=False),
        sa.Column("copy_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("brandkit_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("variant_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("screenshot_path", sa.Text()),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "version"),
    )
    op.create_index("ix_site_versions_ws", "site_versions", ["workspace_id", "version"])

    op.create_table(
        "interest_signals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("site_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("email", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("referrer", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False, server_default="direct"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_interest_ws", "interest_signals", ["workspace_id", "created_at"])

    # --- 39 connectors ----------------------------------------------------
    op.create_table(
        "connections",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_account", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("auth_kind", sa.Text(), nullable=False, server_default="pat"),
        sa.Column("secret_ref", sa.Text()),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("resource_grants_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("declined_scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("webhook_ref", sa.Text()),
        sa.Column("cursors_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("simulated", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_sync_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("health", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("workspace_id", "provider", "external_account"),
    )
    op.create_index("ix_connections_ws", "connections", ["workspace_id", "status"])

    op.create_table(
        "connection_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("target", sa.Text()),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("engagement_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("external_id", sa.Text()),
        sa.Column("bytes_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("denied_reason", sa.Text()),
    )
    op.create_index("ix_conn_events", "connection_events", ["connection_id", "at"])
    _append_only_triggers("connection_events")

    # --- 38 company maps --------------------------------------------------
    op.create_table(
        "company_maps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("confidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("corrections_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("house_style_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("corrected_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "version"),
    )

    # --- 40 workflows -----------------------------------------------------
    op.create_table(
        "workflows",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("sla_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("on_failure_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("autonomy", sa.Text(), nullable=False, server_default="propose"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("paused_reason", sa.Text()),
        sa.Column("cost_cap_run_usd", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("cost_cap_month_usd", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("source", sa.Text(), nullable=False, server_default="described"),
        sa.Column("unmapped_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_workflows_ws", "workflows", ["workspace_id", "status"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("engagement_id", sa.Text()),
        sa.Column("trigger_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("shadow", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verdict", sa.Text()),
        sa.Column("verdict_note", sa.Text()),
        sa.Column("simulated_writes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("outcome_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        # The entire duplicate-run defence (40 §2).
        sa.UniqueConstraint("workflow_id", "idempotency_key"),
    )
    op.create_index("ix_workflow_runs", "workflow_runs", ["workflow_id", "started_at"])

    # --- 42 approvals -----------------------------------------------------
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("risk", sa.Text(), nullable=False, server_default="low"),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_at", sa.DateTime()),
        sa.Column("decision_note", sa.Text()),
        sa.Column("notified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_approvals_queue", "approvals", ["workspace_id", "status", "risk", "created_at"]
    )

    # --- 41 platform surfaces --------------------------------------------
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("surface", sa.Text(), nullable=False, server_default="api"),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_api_tokens_person", "api_tokens", ["person_id"])

    op.create_table(
        "idempotency_records",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=False),
        sa.Column("events_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime()),
    )
    op.create_index(
        "ix_webhook_deliveries", "webhook_deliveries", ["subscription_id", "status"]
    )

    op.create_table(
        "suspicious_content_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )


def _extend_existing() -> None:
    _add_columns("ventures", [sa.Column("workspace_id", sa.Text())])
    _add_columns("activity_lines", [sa.Column("workspace_id", sa.Text())])
    _add_columns(
        "onboarding_runs",
        [
            sa.Column("workspace_id", sa.Text()),
            sa.Column("entry_mode", sa.Text(), nullable=False, server_default="idea"),
        ],
    )
    _add_columns("venture_resources", [sa.Column("workspace_id", sa.Text())])
    _add_columns(
        "planned_tasks",
        [sa.Column("workspace_id", sa.Text()), sa.Column("engagement_id", sa.Text())],
    )
    _add_columns("chat_messages", [sa.Column("workspace_id", sa.Text())])
    # Per-actor audit (42 §5).
    _add_columns(
        "event_log",
        [
            sa.Column("workspace_id", sa.Text()),
            sa.Column("actor_id", sa.Text()),
            sa.Column("actor_kind", sa.Text()),
            sa.Column("on_behalf_of", sa.Text()),
            sa.Column("surface", sa.Text()),
            sa.Column("approval_id", sa.Text()),
            sa.Column("target_ref", sa.Text()),
            sa.Column("outcome", sa.Text()),
            sa.Column("denied_reason", sa.Text()),
        ],
    )


def _backfill() -> None:
    """Venture -> Workspace, plus the local person and their Desk.

    Asserted in-migration: a silent orphan here is invisible until history is
    missing weeks later (46 T10.2, B27).
    """
    bind = op.get_bind()
    now = sa.func.current_timestamp()

    venture_count = bind.execute(sa.text("SELECT COUNT(*) FROM ventures")).scalar_one()

    # 1. The single local founder.
    bind.execute(
        sa.text(
            "INSERT INTO people (id, display_name, email, created_at) "
            "VALUES (:id, 'Founder', NULL, CURRENT_TIMESTAMP)"
        ),
        {"id": LOCAL_PERSON_ID},
    )

    # 2. One workspace per existing venture, carrying its name and autonomy.
    bind.execute(
        sa.text(
            """
            INSERT INTO workspaces
              (id, kind, name, entry_mode, owner_id, tenancy, autonomy_level,
               observe_level, settings_json, created_at, updated_at, archived_at)
            SELECT
              'wsp' || substr(v.id, 4), 'venture', v.name, 'idea', :owner, 'local',
              COALESCE(v.autonomy_level, 'L1'), 'O0', '{}',
              v.created_at, v.updated_at, v.archived_at
            FROM ventures v
            """
        ),
        {"owner": LOCAL_PERSON_ID},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO workspace_members
              (id, workspace_id, person_id, role, can_approve, created_at)
            SELECT 'wsm' || substr(v.id, 4), 'wsp' || substr(v.id, 4), :owner,
                   'owner', 1, v.created_at
            FROM ventures v
            """
        ),
        {"owner": LOCAL_PERSON_ID},
    )
    bind.execute(
        sa.text("UPDATE ventures SET workspace_id = 'wsp' || substr(id, 4)")
    )

    # 3. The personal Desk, where one-off engagements and the Idea Lab live.
    bind.execute(
        sa.text(
            """
            INSERT INTO workspaces
              (id, kind, name, entry_mode, owner_id, tenancy, autonomy_level,
               observe_level, settings_json, created_at, updated_at)
            VALUES (:id, 'desk', 'Desk', 'engagement', :owner, 'local', 'L1', 'O0',
                    '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        ),
        {"id": "wsp_desklocal00", "owner": LOCAL_PERSON_ID},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO workspace_members
              (id, workspace_id, person_id, role, can_approve, created_at)
            VALUES ('wsm_desklocal0', 'wsp_desklocal00', :owner, 'owner', 1,
                    CURRENT_TIMESTAMP)
            """
        ),
        {"owner": LOCAL_PERSON_ID},
    )

    # 4. Backfill workspace_id on everything keyed by venture.
    #    activity_lines is append-only, so its guard triggers must stand down
    #    for this one structural update and be restored immediately after.
    _drop_append_only_triggers("activity_lines")
    try:
        for table in (
            "activity_lines",
            "onboarding_runs",
            "venture_resources",
            "planned_tasks",
            "chat_messages",
        ):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET workspace_id = "
                    f"(SELECT v.workspace_id FROM ventures v "
                    f"WHERE v.id = {table}.venture_id)"
                )
            )
    finally:
        _append_only_triggers("activity_lines")

    # 5. Assertions. Fail the migration rather than leave orphans behind.
    ws_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM workspaces WHERE kind = 'venture'")
    ).scalar_one()
    if ws_count != venture_count:
        raise RuntimeError(
            f"backfill lost rows: {venture_count} ventures -> {ws_count} workspaces"
        )

    unlinked = bind.execute(
        sa.text("SELECT COUNT(*) FROM ventures WHERE workspace_id IS NULL")
    ).scalar_one()
    if unlinked:
        raise RuntimeError(f"{unlinked} ventures have no workspace_id after backfill")

    for table in (
        "activity_lines",
        "onboarding_runs",
        "venture_resources",
        "planned_tasks",
        "chat_messages",
    ):
        orphans = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} t WHERE t.workspace_id IS NULL "
                f"AND EXISTS (SELECT 1 FROM ventures v WHERE v.id = t.venture_id)"
            )
        ).scalar_one()
        if orphans:
            raise RuntimeError(f"{table}: {orphans} rows failed workspace backfill")

    _ = now  # keep sa.func import meaningful for future dialects


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    _drop_append_only_triggers("connection_events")
    _drop_append_only_triggers("boundary_ledger")

    for table in (
        "suspicious_content_events",
        "webhook_deliveries",
        "webhook_subscriptions",
        "idempotency_records",
        "api_tokens",
        "approvals",
        "workflow_runs",
        "workflows",
        "company_maps",
        "connection_events",
        "connections",
        "interest_signals",
        "site_versions",
        "opportunities",
        "opportunity_slates",
        "engagements",
        "boundary_ledger",
        "invites",
        "workspace_members",
        "workspaces",
        "people",
    ):
        op.drop_table(table)

    for table, columns in (
        ("ventures", ["workspace_id"]),
        ("activity_lines", ["workspace_id"]),
        ("onboarding_runs", ["workspace_id", "entry_mode"]),
        ("venture_resources", ["workspace_id"]),
        ("planned_tasks", ["workspace_id", "engagement_id"]),
        ("chat_messages", ["workspace_id"]),
        (
            "event_log",
            [
                "workspace_id",
                "actor_id",
                "actor_kind",
                "on_behalf_of",
                "surface",
                "approval_id",
                "target_ref",
                "outcome",
                "denied_reason",
            ],
        ),
    ):
        for col in columns:
            op.drop_column(table, col)
