"""Add credit metering, identity link, and admin-control tables.

Revision ID: b1c2d3e4f5a6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-17
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spec §4.1 — values stored as JSON in value_json.
_SEEDED_POLICIES: tuple[tuple[str, object], ...] = (
    ("default_venture_credits", 5000),
    ("max_grant_credits_support", 2000),
    ("max_grant_credits_finance", 5000),
    ("dual_control_threshold", 5000),
    ("reservation_ttl_seconds", 900),
    ("web_search_credits", 2),
    ("web_fetch_credits", 1),
    ("enforcement_enabled", True),
)


def _append_only_triggers(table: str) -> None:
    """Block UPDATE/DELETE. Spec §4.5."""
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


def _create_hash_chained_table(table: str, extra_sql: str) -> None:
    """``seq`` AUTOINCREMENT + TEXT ``id`` UNIQUE. SQLite requires INTEGER PK."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        extra_sql = extra_sql.replace("DATETIME", "TIMESTAMP")
        op.execute(
            f"""
            CREATE TABLE {table} (
              seq BIGSERIAL PRIMARY KEY,
              id TEXT NOT NULL UNIQUE,
              {extra_sql}
            )
            """
        )
    else:
        op.execute(
            f"""
            CREATE TABLE {table} (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              id TEXT NOT NULL UNIQUE,
              {extra_sql}
            )
            """
        )


def upgrade() -> None:
    op.create_table(
        "credit_accounts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False, server_default="venture"),
        sa.Column("venture_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("owner_person_id", sa.Text()),
        sa.Column("granted_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("spent_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venture_id", name="uq_credit_accounts_venture_id"),
        sa.CheckConstraint(
            "granted_micro >= 0", name="ck_credit_accounts_granted_micro"
        ),
        sa.CheckConstraint(
            "spent_micro >= 0", name="ck_credit_accounts_spent_micro"
        ),
        sa.CheckConstraint(
            "reserved_micro >= 0", name="ck_credit_accounts_reserved_micro"
        ),
        sa.CheckConstraint(
            "spent_micro + reserved_micro <= granted_micro",
            name="ck_credit_accounts_no_overspend",
        ),
    )

    _create_hash_chained_table(
        "credit_ledger",
        """
              account_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              amount_micro BIGINT NOT NULL,
              available_after_micro BIGINT NOT NULL,
              reason TEXT NOT NULL,
              actor_kind TEXT NOT NULL,
              actor_id TEXT,
              purpose TEXT,
              model TEXT,
              provider TEXT,
              prompt_tokens INTEGER,
              completion_tokens INTEGER,
              reservation_id TEXT,
              request_id TEXT,
              idempotency_key TEXT NOT NULL UNIQUE,
              meta_json TEXT NOT NULL DEFAULT '{}',
              prev_hash TEXT NOT NULL,
              entry_hash TEXT NOT NULL,
              created_at DATETIME NOT NULL
        """,
    )
    _append_only_triggers("credit_ledger")

    op.create_table(
        "credit_reservations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("settled_micro", sa.BigInteger()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("request_id", sa.Text()),
        sa.Column("clamped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime()),
        sa.CheckConstraint(
            "amount_micro > 0", name="ck_credit_reservations_amount"
        ),
    )
    op.create_index(
        "ix_credit_reservations_status_expires",
        "credit_reservations",
        ["status", "expires_at"],
    )

    op.create_table(
        "credit_policies",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.Text()),
    )

    op.create_table(
        "credit_grant_approvals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("target_email", sa.Text()),
        sa.Column("target_account_id", sa.Text()),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text()),
        sa.Column("applied_ledger_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("totp_secret_enc", sa.Text()),
        sa.Column("totp_enrolled_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Text()),
        sa.Column("last_login_at", sa.DateTime()),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime()),
        sa.UniqueConstraint("email", name="uq_admin_users_email"),
    )

    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("admin_user_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("ip", sa.Text()),
        sa.Column("user_agent", sa.Text()),
        sa.UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
    )

    _create_hash_chained_table(
        "admin_audit_log",
        """
              actor_admin_id TEXT,
              actor_email TEXT,
              action TEXT NOT NULL,
              target_kind TEXT,
              target_id TEXT,
              before_json TEXT,
              after_json TEXT,
              amount_micro BIGINT,
              ip TEXT,
              user_agent TEXT,
              request_id TEXT,
              outcome TEXT NOT NULL,
              reason TEXT,
              prev_hash TEXT NOT NULL,
              entry_hash TEXT NOT NULL,
              created_at DATETIME NOT NULL
        """,
    )
    _append_only_triggers("admin_audit_log")

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text()),
        sa.Column("amount_cents", sa.Integer()),
        sa.Column("currency", sa.Text()),
        sa.Column("credits_micro", sa.BigInteger()),
        sa.Column("account_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("processed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "provider_event_id", name="uq_payment_events_provider_event_id"
        ),
    )

    op.create_table(
        "person_identities",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "person_id", sa.Text(), sa.ForeignKey("people.id"), nullable=False
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("subject", name="uq_person_identities_subject"),
    )
    op.create_index("ix_person_identities_email", "person_identities", ["email"])

    bind = op.get_bind()
    for key, value in _SEEDED_POLICIES:
        bind.execute(
            sa.text(
                "INSERT INTO credit_policies (key, value_json, updated_at, updated_by) "
                "VALUES (:key, :value_json, CURRENT_TIMESTAMP, 'system')"
            ),
            {"key": key, "value_json": json.dumps(value)},
        )


def downgrade() -> None:
    _drop_append_only_triggers("admin_audit_log")
    _drop_append_only_triggers("credit_ledger")

    op.drop_index("ix_person_identities_email", table_name="person_identities")
    op.drop_table("person_identities")
    op.drop_table("payment_events")
    op.drop_table("admin_audit_log")
    op.drop_table("admin_sessions")
    op.drop_table("admin_users")
    op.drop_table("credit_grant_approvals")
    op.drop_table("credit_policies")
    op.drop_index(
        "ix_credit_reservations_status_expires", table_name="credit_reservations"
    )
    op.drop_table("credit_reservations")
    op.drop_table("credit_ledger")
    op.drop_table("credit_accounts")
