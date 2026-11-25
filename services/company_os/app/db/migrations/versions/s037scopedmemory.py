"""Private conversations, retained raw history, and shared PostgreSQL memory."""
import hashlib
import sqlalchemy as sa
from alembic import op

revision = "s037scopedmemory"
down_revision = "s036touchventure"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chat_conversations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("creator_person_id", sa.Text()),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("creation_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("visibility IN ('private', 'legacy_shared')"),
        sa.CheckConstraint("visibility != 'private' OR creator_person_id IS NOT NULL"))
    op.create_index("ix_chat_conversation_owner", "chat_conversations", ["workspace_id", "venture_id", "creator_person_id"])
    op.add_column("chat_messages", sa.Column("conversation_id", sa.Text()))
    op.add_column("chat_messages", sa.Column("author_person_id", sa.Text()))
    bind = op.get_bind()
    # No text, action, receipt, idempotency key, or timestamp is rewritten.
    for row in bind.execute(sa.text("SELECT id, workspace_id, created_at FROM ventures")).mappings():
        cid = "cnv_" + hashlib.sha256(("legacy:" + row["id"]).encode()).hexdigest()[:32]
        bind.execute(sa.text("INSERT INTO chat_conversations (id,workspace_id,venture_id,visibility,title,creation_key,created_at,updated_at) VALUES (:id,:wid,:vid,'legacy_shared','Earlier shared chat',:key,:created,:created)"),
                     {"id": cid, "wid": row["workspace_id"], "vid": row["id"], "key": "legacy:" + row["id"], "created": row["created_at"]})
        bind.execute(sa.text("UPDATE chat_messages SET conversation_id=:cid, workspace_id=:wid WHERE venture_id=:vid"),
                     {"cid": cid, "wid": row["workspace_id"], "vid": row["id"]})
    op.create_index("ix_chat_message_conversation", "chat_messages", ["conversation_id", "created_at", "id"])
    with op.batch_alter_table("chat_messages") as batch:
        batch.create_foreign_key("fk_chat_message_conversation", "chat_conversations", ["conversation_id"], ["id"])
    op.create_table("chat_context_checkpoints",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("chat_conversations.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("watermark_id", sa.Text(), nullable=False),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "version"))
    op.create_table("institutional_memories",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("venture_id", "dedupe_key"))
    op.create_index("ix_institutional_scope", "institutional_memories", ["workspace_id", "venture_id"])
    op.create_table("memory_working_entries",
        sa.Column("scope_key", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text()), sa.Column("venture_id", sa.Text()),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    # Downgrade removes the new partitions; callers must export private chats first.
    op.drop_table("memory_working_entries")
    op.drop_table("institutional_memories")
    op.drop_table("chat_context_checkpoints")
    op.drop_index("ix_chat_message_conversation", table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_constraint("fk_chat_message_conversation", type_="foreignkey")
    op.drop_column("chat_messages", "author_person_id")
    op.drop_column("chat_messages", "conversation_id")
    op.drop_table("chat_conversations")
