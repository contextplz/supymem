"""Read connected systems into a redacted inventory (38 §4)."""

from __future__ import annotations

from typing import Any

from app.connectors.events import record as record_event
from app.connectors.protocol import ConnectionView
from app.connectors.registry import get_connector
from app.connectors.service import list_connections
from app.discovery.redact import may_prompt, redact_inventory, safe_text
from app.workspaces.resolve import LOCAL_PERSON_ID


async def scan_workspace(
    workspace_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
) -> dict[str, Any]:
    """Produce a redacted inventory over every active connection."""
    connections = list_connections(workspace_id)
    inventories: list[dict[str, Any]] = []
    for con in connections:
        if con.status != "active":
            continue
        inventories.append(
            await scan_connection(con, actor_id=actor_id)
        )
    return redact_inventory(
        {
            "workspace_id": workspace_id,
            "connections": inventories,
            "connection_count": len(inventories),
        }
    )


async def scan_connection(
    con: ConnectionView,
    *,
    actor_id: str = LOCAL_PERSON_ID,
) -> dict[str, Any]:
    """Given a Connection, list resources and sample safe file content."""
    connector = get_connector(con.provider, simulated=con.simulated)
    resources: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []

    # Prefer the fake/local perform path so tests never need a live warden.
    try:
        page = await connector._perform_list(con, "repo", None)  # noqa: SLF001
        record_event(
            connection_id=con.id,
            workspace_id=con.workspace_id,
            direction="read",
            operation="list_repos",
            actor=actor_id,
            outcome="ok",
            target=con.external_account,
            bytes_in=page.bytes_in,
            simulated=con.simulated,
        )
        for item in page.items:
            resources.append(
                {
                    "kind": item.kind,
                    "ref": item.ref,
                    "display": item.display,
                    "detail": _safe_detail(item.detail),
                }
            )
    except Exception as exc:  # noqa: BLE001
        record_event(
            connection_id=con.id,
            workspace_id=con.workspace_id,
            direction="read",
            operation="list_repos",
            actor=actor_id,
            outcome="error",
            target=con.external_account,
            denied_reason=str(exc),
            simulated=con.simulated,
        )
        return {
            "connection_id": con.id,
            "provider": con.provider,
            "external_account": con.external_account,
            "error": str(exc),
            "resources": [],
            "files": [],
            "dropped": [],
        }

    # Sample README-like content from the fixture when available.
    for candidate in ("README.md", "readme.md", "pyproject.toml", "package.json"):
        try:
            from app.connectors.protocol import ResourceRef

            result = await connector._perform_read(  # noqa: SLF001
                con, ResourceRef(kind="file", ref=candidate)
            )
        except Exception:
            continue
        ok, reason = may_prompt(candidate, result.content)
        if not ok:
            dropped.append({"path": candidate, "reason": reason or "dropped"})
            continue
        files.append(
            {
                "path": candidate,
                "content": safe_text(result.content)[:4000],
                "bytes": result.bytes_in,
            }
        )
        record_event(
            connection_id=con.id,
            workspace_id=con.workspace_id,
            direction="read",
            operation="read_file",
            actor=actor_id,
            outcome="ok",
            target=candidate,
            bytes_in=result.bytes_in,
            simulated=con.simulated,
        )

    return redact_inventory(
        {
            "connection_id": con.id,
            "provider": con.provider,
            "external_account": con.external_account,
            "simulated": con.simulated,
            "resources": resources,
            "files": files,
            "dropped": dropped,
        }
    )


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    return {k: v for k, v in detail.items() if k.lower() not in {"token", "secret", "password"}}
