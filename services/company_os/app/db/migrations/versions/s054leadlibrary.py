"""Shared Lead Library of licensed B2B contacts."""
import sqlalchemy as sa
from alembic import op

revision = "s054leadlibrary"
down_revision = "s053waitlistprops"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("lead_library_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("external_lead_id", sa.Text(), nullable=False, unique=True),
        sa.Column("person_name", sa.Text(), nullable=False),
        sa.Column("role_title", sa.Text()), sa.Column("company", sa.Text()), sa.Column("company_domain", sa.Text()),
        sa.Column("website", sa.Text()), sa.Column("linkedin_url", sa.Text()), sa.Column("x_url", sa.Text()),
        sa.Column("email", sa.Text()), sa.Column("email_hash", sa.Text()),
        sa.Column("email_status", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text()), sa.Column("phone_status", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text()), sa.Column("region", sa.Text()), sa.Column("city", sa.Text()),
        sa.Column("geography_raw", sa.Text()),
        sa.Column("lead_type", sa.Text(), nullable=False), sa.Column("function", sa.Text(), nullable=False),
        sa.Column("seniority", sa.Text(), nullable=False), sa.Column("company_stage", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float()), sa.Column("fit_score", sa.Integer()), sa.Column("quality_score", sa.Float()),
        sa.Column("usable_channels", sa.Text(), nullable=False), sa.Column("quality_flags", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()), sa.Column("source_host", sa.Text()), sa.Column("discovery_method", sa.Text()),
        sa.Column("license_source", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text()), sa.Column("collected_at", sa.Text()),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False), sa.Column("status_reason", sa.Text()),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('active','suppressed','retired')", name="ck_lead_library_entry_status"))
    for name, columns in (
        ("ix_lead_library_entries_type_country", ["lead_type", "country_code"]),
        ("ix_lead_library_entries_function_seniority", ["function", "seniority"]),
        ("ix_lead_library_entries_status_rank", ["status", "fit_score"]),
        ("ix_lead_library_entries_email_hash", ["email_hash"]),
        ("ix_lead_library_entries_company_domain", ["company_domain"]),
    ):
        op.create_index(name, "lead_library_entries", columns)
    op.create_table("lead_library_tags",
        sa.Column("entry_id", sa.Text(), primary_key=True),
        sa.Column("tag_type", sa.Text(), primary_key=True),
        sa.Column("tag", sa.Text(), primary_key=True))
    op.create_index("ix_lead_library_tags_lookup", "lead_library_tags", ["tag_type", "tag", "entry_id"])
    op.create_table("lead_library_usage",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("venture_id", sa.Text()),
        sa.Column("contacted_at", sa.DateTime(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False))
    op.create_index("ix_lead_library_usage_entry_time", "lead_library_usage", ["entry_id", "contacted_at"])
    op.create_index("ix_lead_library_usage_workspace", "lead_library_usage", ["workspace_id", "venture_id"])


def downgrade():
    for name in ("lead_library_usage", "lead_library_tags", "lead_library_entries"):
        op.drop_table(name)
