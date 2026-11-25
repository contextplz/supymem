"""Add indexed device execution leases without changing reviewed manifests."""
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = "s043deviceleases"
down_revision = "s042walletbridge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("venture_documents", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.create_index("ix_venture_documents_lease", "venture_documents", ["kind", "lease_expires_at"])
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, provenance_json FROM venture_documents "
                                "WHERE kind = 'device_work_order_v1'")).all()
    for order_id, provenance in rows:
        body = json.loads(provenance or "{}")
        if body.get("state") not in {"claimed", "cancelled"} or not body.get("claimed_at") or body.get("closed_at"):
            continue
        claimed = datetime.fromisoformat(body["claimed_at"])
        expires = claimed + timedelta(hours=2)
        body["lease_expires_at"] = expires.isoformat()
        bind.execute(sa.text("UPDATE venture_documents SET lease_expires_at = :expires, "
                             "provenance_json = :body WHERE id = :id"),
                     {"id": order_id, "expires": expires.astimezone(timezone.utc).replace(tzinfo=None)
                      if expires.tzinfo else expires, "body": json.dumps(body)})


def downgrade():
    op.drop_index("ix_venture_documents_lease", table_name="venture_documents")
    op.drop_column("venture_documents", "lease_expires_at")
