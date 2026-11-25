"""Index credit_ledger by account and time for the founder spend view (additive).

Integration requires s058memsearch, s059scaleidx and s061codingconsent from their owning branches.
"""
from alembic import op
from sqlalchemy import text

revision = "s062spend"
down_revision = "s061codingconsent"
branch_labels = None
depends_on = None


def _index_valid():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        row = connection.execute(text("SELECT indisvalid, indrelid = to_regclass('credit_ledger'), "
            "pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indexrelid = to_regclass('ix_credit_ledger_account_created')")).first()
        if row and row[0] and (not row[1] or not row[2].startswith("CREATE INDEX ")
                or row[2].partition(" USING ")[2].split() != ["btree", "(account_id,", "created_at)"]):
            raise RuntimeError("spend_index_definition_mismatch")
        return row[0] if row else None
    definition = connection.scalar(text("SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ix_credit_ledger_account_created'"))
    if definition and definition.split() != (
            "CREATE INDEX ix_credit_ledger_account_created ON credit_ledger (account_id, created_at)".split()):
        raise RuntimeError("spend_index_definition_mismatch")
    return True if definition else None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            if _index_valid() is False:
                op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_credit_ledger_account_created")
            op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_credit_ledger_account_created "
                       "ON credit_ledger (account_id, created_at)")
            if _index_valid() is not True:
                raise RuntimeError("spend_index_invalid")
    else:
        _index_valid()
        op.create_index("ix_credit_ledger_account_created", "credit_ledger", ["account_id", "created_at"], if_not_exists=True)


def downgrade():
    op.drop_index("ix_credit_ledger_account_created", table_name="credit_ledger", if_exists=True)
