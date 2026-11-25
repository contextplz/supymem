"""Merge outreach/ads/GTM plans with the deployed import-intake lineage.

Both branches descend from the existing community schema. Their subsequent
changes are additive and independent; retain their revision identities so
databases already at either head can apply only the missing branch.
"""

revision = "u6v7w8x9y0z1"
down_revision = ("s010importintake", "t5u6v7w8x9y0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
