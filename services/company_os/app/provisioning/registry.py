"""Select local vs real adapters (28 §3)."""

from __future__ import annotations

from typing import Any

from app.provisioning.adapters import (
    db_local,
    domain_local,
    email_local,
    host_local,
    repo_local,
    workspace_local,
)
from app.provisioning.credentials import credentials_armed, provisioning_mode
from app.provisioning.protocol import Adapter, Resource

LOCAL_ADAPTERS: dict[str, Adapter] = {
    "workspace": workspace_local.WorkspaceLocal(),
    "repo": repo_local.RepoLocal(),
    "database": db_local.DbLocal(),
    "email": email_local.EmailLocal(),
    "hosting": host_local.HostLocal(),
    "domain": domain_local.DomainLocal(),
    "sandbox": workspace_local.SandboxLocal(),
}

# Real adapters not wired in local CI — empty preference lists.
REAL_ADAPTERS: dict[str, list[Adapter]] = {
    "workspace": [],
    "repo": [],
    "database": [],
    "email": [],
    "hosting": [],
    "domain": [],
    "sandbox": [],
    "payments": [],
}


async def select(resource: Resource | str, ctx: Any) -> Adapter:
    if provisioning_mode() == "local":
        return LOCAL_ADAPTERS[resource]
    for adapter in REAL_ADAPTERS.get(resource, []):
        if not credentials_armed(ctx, adapter.required_credentials):
            continue
        pf = await adapter.preflight(ctx)
        if pf.ok:
            return adapter
    return LOCAL_ADAPTERS[resource]
