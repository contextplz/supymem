"""GitHub fine-grained token permissions, one per feature (39 §5).

These are the permission names GitHub itself uses on a fine-grained personal
access token or GitHub App (``metadata``, ``contents``, ``pull_requests``,
``issues``, ``actions``, ``repository_hooks``), each at the lowest level that
makes its feature work. Deliberately absent: ``administration``, ``workflows``,
``deployments``, ``secrets``, ``environments`` and anything organisation-wide.
"""

from __future__ import annotations

from app.connectors.protocol import (
    METADATA,
    READ,
    Capability,
    ReadSpec,
    ScopeSpec,
)

METADATA_READ = "metadata:read"
CONTENTS_READ = "contents:read"
PULLS_READ = "pull_requests:read"
ISSUES_READ = "issues:read"
ACTIONS_READ = "actions:read"
CONTENTS_WRITE = "contents:write"
PULLS_WRITE = "pull_requests:write"
ISSUES_WRITE = "issues:write"
HOOKS_WRITE = "repository_hooks:write"

SCOPES: dict[str, ScopeSpec] = {
    METADATA_READ: ScopeSpec(METADATA_READ, writes=False, required_for=("list_repos",)),
    CONTENTS_READ: ScopeSpec(
        CONTENTS_READ,
        writes=False,
        required_for=("read_tree", "read_file", "list_commits"),
    ),
    PULLS_READ: ScopeSpec(PULLS_READ, writes=False, required_for=("list_pulls",)),
    ISSUES_READ: ScopeSpec(ISSUES_READ, writes=False, required_for=("list_issues",)),
    ACTIONS_READ: ScopeSpec(
        ACTIONS_READ, writes=False, required_for=("list_check_runs",)
    ),
    CONTENTS_WRITE: ScopeSpec(
        CONTENTS_WRITE, writes=True, required_for=("create_branch", "commit_file")
    ),
    PULLS_WRITE: ScopeSpec(
        PULLS_WRITE,
        writes=True,
        required_for=("open_draft_pr", "open_pr", "comment_pr"),
    ),
    ISSUES_WRITE: ScopeSpec(
        ISSUES_WRITE, writes=True, required_for=("comment_issue",)
    ),
    HOOKS_WRITE: ScopeSpec(
        HOOKS_WRITE, writes=True, required_for=("register_webhook",)
    ),
}

# Listing repositories is metadata (O1); anything with file or issue content in
# it is a read (O2). The split is what makes an O1 connection genuinely safe.
READS: tuple[ReadSpec, ...] = (
    ReadSpec("list_repos", "repo", METADATA_READ, METADATA),
    ReadSpec("read_tree", "tree", CONTENTS_READ, READ),
    ReadSpec("read_file", "file", CONTENTS_READ, READ),
    ReadSpec("list_commits", "commit", CONTENTS_READ, READ),
    ReadSpec("list_pulls", "pull_request", PULLS_READ, READ),
    ReadSpec("list_issues", "issue", ISSUES_READ, READ),
    ReadSpec("list_check_runs", "check_run", ACTIONS_READ, READ),
)

CAPABILITIES: tuple[Capability, ...] = (
    Capability("create_branch", "create_branch", CONTENTS_WRITE),
    Capability("commit_file", "commit_file", CONTENTS_WRITE),
    Capability("open_draft_pr", "open_draft_pr", PULLS_WRITE),
    Capability("open_pr", "open_pr", PULLS_WRITE),
    Capability("comment_pr", "comment_pr", PULLS_WRITE),
    Capability("comment_issue", "comment_issue", ISSUES_WRITE),
    Capability("register_webhook", "register_webhook", HOOKS_WRITE),
)
