"""Internal facts-only startup catalogue index; descriptions are not API data."""
from alembic import op
import sqlalchemy as sa

revision = "s046lootdrop"
down_revision = "s045tasks"
branch_labels = None
depends_on = None


def upgrade():
    from app.db.supymem_models import LootDropEntry
    LootDropEntry.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    bind = op.get_bind()
    if "loot_drop_entries" not in sa.inspect(bind).get_table_names():
        return
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE loot_drop_entries IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text("SELECT 1 FROM loot_drop_entries LIMIT 1")).first():
        raise RuntimeError("loot_drop_index_prevents_downgrade")
    op.drop_table("loot_drop_entries")
