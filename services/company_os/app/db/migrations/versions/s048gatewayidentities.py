"""Persist admitted federated identities separately from password accounts."""
import sqlalchemy as sa
from alembic import op

revision = "s048gatewayidentities"
down_revision = "s047credentialepoch"
branch_labels = None
depends_on = None


def upgrade():
    from app.db.supymem_models import GatewayIdentity

    GatewayIdentity.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    bind = op.get_bind()
    if "gateway_identities" not in sa.inspect(bind).get_table_names():
        return
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE gateway_identities IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text("SELECT 1 FROM gateway_identities LIMIT 1")).first():
        raise RuntimeError("gateway_identity_history_prevents_downgrade")
    op.drop_table("gateway_identities")
