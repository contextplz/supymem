"""Add credit_orders and extend payment_events for top-up webhooks.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-19
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREDIT_PACKS: list[dict[str, object]] = [
    {"sku": "cr_500", "credits": 500, "usd": 500, "inr": 44900, "label": "Starter"},
    {"sku": "cr_1000", "credits": 1000, "usd": 1000, "inr": 89900, "label": "Builder"},
    {
        "sku": "cr_2000",
        "credits": 2000,
        "usd": 2000,
        "inr": 179900,
        "label": "Studio",
        "badge": "Popular",
    },
    {
        "sku": "cr_5000",
        "credits": 5000,
        "usd": 5000,
        "inr": 439900,
        "label": "Scale",
        "bonus_credits": 250,
    },
]


def upgrade() -> None:
    op.create_table(
        "credit_orders",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("person_id", sa.Text()),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("bonus_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_order_id", sa.Text()),
        sa.Column("provider_ref", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("ledger_entry_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()),
        sa.UniqueConstraint(
            "provider",
            "provider_order_id",
            name="uq_credit_orders_provider_order",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_credit_orders_idempotency_key"
        ),
        sa.CheckConstraint(
            "credits > 0 AND amount_minor > 0",
            name="ck_credit_orders_positive_amounts",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'expired', 'refunded')",
            name="ck_credit_orders_status",
        ),
    )

    op.add_column("payment_events", sa.Column("order_id", sa.Text()))
    op.add_column(
        "payment_events",
        sa.Column(
            "signature_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO credit_policies (key, value_json, updated_at, updated_by) "
            "VALUES (:key, :value_json, CURRENT_TIMESTAMP, 'system')"
        ),
        {"key": "credit_packs", "value_json": json.dumps(_CREDIT_PACKS)},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM credit_policies WHERE key = :key"),
        {"key": "credit_packs"},
    )
    with op.batch_alter_table("payment_events") as batch:
        batch.drop_column("signature_verified")
        batch.drop_column("order_id")
    op.drop_table("credit_orders")
