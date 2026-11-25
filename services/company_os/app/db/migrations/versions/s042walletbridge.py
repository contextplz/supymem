"""Add wallet custody fields and phase marker without moving any value.

Deploy and qualify the matching Core/worker bridge before merging cutover.
Legacy publication constraints and all balances/grants remain untouched.
"""
import sqlalchemy as sa
from alembic import op

revision = "s042walletbridge"
down_revision = "s041refreshgrace"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("credit_reservations")}
    for name in ("venture_id", "task_invocation"):
        if name not in columns:
            op.add_column("credit_reservations", sa.Column(name, sa.Text(), nullable=True))
    if "uq_credit_accounts_person" not in {i["name"] for i in inspector.get_indexes("credit_accounts")}:
        op.create_index("uq_credit_accounts_person", "credit_accounts", ["owner_person_id"], unique=True,
                        postgresql_where=sa.text("scope = 'person'"), sqlite_where=sa.text("scope = 'person'"))
    # Never overwrite an adopted person phase. No environment controls it.
    op.execute(sa.text("INSERT INTO credit_policies (key,value_json,updated_at,updated_by) "
        "VALUES ('founder_wallet_phase',:phase,CURRENT_TIMESTAMP,'wallet-schema-expansion') "
        "ON CONFLICT (key) DO NOTHING").bindparams(phase='"legacy"'))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM credit_accounts WHERE scope = 'person'")).scalar():
        raise RuntimeError("wallet bridge retains person wallet history")
    if bind.execute(sa.text("SELECT COUNT(*) FROM credit_reservations WHERE task_invocation IS NOT NULL")).scalar():
        raise RuntimeError("wallet bridge retains task usage custody")
    op.drop_index("uq_credit_accounts_person", table_name="credit_accounts")
    op.drop_column("credit_reservations", "task_invocation")
    op.drop_column("credit_reservations", "venture_id")
    op.execute("DELETE FROM credit_policies WHERE key='founder_wallet_phase'")
