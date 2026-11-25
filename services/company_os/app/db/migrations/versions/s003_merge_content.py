"""Merge the platform foundation and content/GTM migration branches.

Both existing branch histories are preserved so installations at either head
can upgrade through the other branch before reaching a single combined head.
"""

revision = "s003mergecontent"
down_revision = ("s002credits", "p0k1l2m3n4o5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
