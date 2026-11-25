"""Linear API scopes, one per feature (39 §5)."""

from __future__ import annotations

from app.connectors.protocol import (
    METADATA,
    READ,
    Capability,
    ReadSpec,
    ScopeSpec,
)

TEAMS_READ = "teams:read"
ISSUES_READ = "issues:read"
ISSUES_WRITE = "issues:write"
COMMENTS_WRITE = "comments:write"

SCOPES: dict[str, ScopeSpec] = {
    TEAMS_READ: ScopeSpec(TEAMS_READ, writes=False, required_for=("list_teams",)),
    ISSUES_READ: ScopeSpec(
        ISSUES_READ, writes=False, required_for=("list_issues", "read_issue")
    ),
    ISSUES_WRITE: ScopeSpec(ISSUES_WRITE, writes=True, required_for=("create_issue",)),
    COMMENTS_WRITE: ScopeSpec(
        COMMENTS_WRITE, writes=True, required_for=("comment_issue",)
    ),
}

READS: tuple[ReadSpec, ...] = (
    ReadSpec("list_teams", "team", TEAMS_READ, METADATA),
    ReadSpec("list_issues", "issue", ISSUES_READ, READ),
    ReadSpec("read_issue", "issue", ISSUES_READ, READ),
)

CAPABILITIES: tuple[Capability, ...] = (
    Capability("create_issue", "create_issue", ISSUES_WRITE),
    Capability("comment_issue", "comment_issue", COMMENTS_WRITE),
)

__all__ = [
    "CAPABILITIES",
    "COMMENTS_WRITE",
    "ISSUES_READ",
    "ISSUES_WRITE",
    "READS",
    "SCOPES",
    "TEAMS_READ",
]
