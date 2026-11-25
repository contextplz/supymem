"""Connection lifecycle: create, list, revoke (39 §8).

Connections are never torn down by provisioning. Revoke drops our webhooks and
vault refs; their resources stay put.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.audit.writer import audit
from app.authz.gate import Denied, check
from app.config import is_local_env
from app.connectors import events
from app.connectors.protocol import ConnectionView
from app.connectors.registry import get_connector
from app.connectors.secrets import store as secret_store
from app.db.models import Connection
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow
from core.ids import new_id


class ConnectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ConnectionRecord:
    view: ConnectionView

    def to_dict(self) -> dict[str, Any]:
        return self.view.to_dict()


def _parse_scopes(raw: str) -> tuple[str, ...]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ()
    if isinstance(data, list):
        out: list[str] = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("scope"):
                out.append(str(item["scope"]))
        return tuple(out)
    return ()


def _parse_list(raw: str) -> tuple[str, ...]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ()
    if isinstance(data, list):
        return tuple(str(x) for x in data)
    return ()


def row_to_view(row: Connection) -> ConnectionView:
    return ConnectionView(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=row.provider,
        external_account=row.external_account,
        status=row.status,
        auth_kind=row.auth_kind,
        secret_ref=row.secret_ref,
        scopes=_parse_scopes(row.scopes_json),
        declined_scopes=_parse_list(row.declined_scopes_json),
        resource_grants=_parse_list(row.resource_grants_json),
        cursors=_json_dict(row.cursors_json),
        simulated=bool(row.simulated),
        health=row.health or "unknown",
        webhook_ref=row.webhook_ref,
        last_error=row.last_error,
        last_sync_at=row.last_sync_at.isoformat() if row.last_sync_at else None,
    )


def _json_dict(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def get_connection(connection_id: str) -> ConnectionView | None:
    factory = db()
    with factory() as session:
        row = session.get(Connection, connection_id)
        return row_to_view(row) if row else None


def list_connections(workspace_id: str) -> list[ConnectionView]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(Connection)
            .where(
                Connection.workspace_id == workspace_id,
                Connection.status != "revoked",
            )
            .order_by(Connection.created_at.desc())
        ).all()
        return [row_to_view(r) for r in rows]


async def create_connection(
    *,
    workspace_id: str,
    provider: str,
    auth_kind: str = "pat",
    external_account: str | None = None,
    token: str | None = None,
    scopes: list[str] | None = None,
    resource_grants: list[str] | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> ConnectionView:
    """Create an active connection. Local mode completes via the fake adapter."""
    decision = await check(
        "connect_systems",
        workspace_id=workspace_id,
        actor_id=actor_id,
        surface=surface,
        consult_warden=False,
    )
    if not decision.allowed:
        raise Denied(decision)

    try:
        connector = get_connector(provider)
    except KeyError as exc:
        raise ConnectionError("unknown_provider", f"Unknown provider: {provider}") from exc

    if not (token or "").strip() and is_local_env():
        # Local dev convenience only. Hosted must never adopt the platform's own token.
        env_names = {"github": "GITHUB_TOKEN", "linear": "LINEAR_API_KEY"}
        token = os.environ.get(env_names.get(provider, ""), "") or token
    payload = {
        "token": token or "",
        "external_account": external_account or "",
    }
    completed = connector.auth_complete(payload)
    account = str(completed["external_account"])
    secret_value = str(completed["secret_value"])
    ref = secret_store().put(
        workspace_id=workspace_id,
        provider=provider,
        key="credential",
        value=secret_value,
    )

    granted = scopes or [connector.baseline_scope]
    now = utcnow()
    scope_rows = [
        {
            "scope": s,
            "granted_at": now.isoformat(),
            "granted_by": actor_id,
            "reason": "connect",
        }
        for s in granted
    ]
    grants = list(resource_grants or [])
    if not grants and account:
        # Fake fixtures use org/repo style; grant the account as a repo root.
        grants = [f"{account}/demo"]

    row_id = new_id("con")
    factory = db()
    with factory() as session:
        existing = session.scalars(
            select(Connection).where(
                Connection.workspace_id == workspace_id,
                Connection.provider == provider,
                Connection.external_account == account,
            )
        ).first()
        if existing is not None and existing.status != "revoked":
            raise ConnectionError(
                "already_connected",
                f"{provider} account {account} is already connected.",
            )
        row = Connection(
            id=row_id,
            workspace_id=workspace_id,
            provider=provider,
            external_account=account,
            status="active",
            auth_kind=auth_kind or connector.auth_kind,
            secret_ref=ref,
            scopes_json=json.dumps(scope_rows),
            resource_grants_json=json.dumps(grants),
            declined_scopes_json="[]",
            cursors_json="{}",
            simulated=1 if connector.simulated else 0,
            health="ok",
            created_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        view = row_to_view(row)

    # Connecting a system grants O1 (38 §5).
    from app.workspaces.service import get_workspace, set_observe_level

    ws = get_workspace(workspace_id)
    if ws is not None and ws.observe_level == "O0":
        set_observe_level(workspace_id, "O1")

    audit(
        "connection.created",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={
            "provider": provider,
            "external_account": account,
            "simulated": view.simulated,
        },
    )
    events.record(
        connection_id=view.id,
        workspace_id=workspace_id,
        direction="read",
        operation="connect",
        actor=actor_id,
        outcome="ok",
        target=account,
        simulated=view.simulated,
    )
    return view


async def revoke_connection(
    connection_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> ConnectionView:
    factory = db()
    with factory() as session:
        row = session.get(Connection, connection_id)
        if row is None:
            raise ConnectionError("not_found", "That connection doesn't exist.")
        decision = await check(
            "connect_systems",
            workspace_id=row.workspace_id,
            actor_id=actor_id,
            surface=surface,
            consult_warden=False,
        )
        if not decision.allowed:
            raise Denied(decision)
        if row.status == "revoked":
            return row_to_view(row)
        view = row_to_view(row)
        try:
            connector = get_connector(view.provider, simulated=view.simulated)
            connector.revoke(view)
        except Exception:
            pass
        if row.secret_ref:
            try:
                secret_store().forget(row.secret_ref)
            except Exception:
                pass
        row.status = "revoked"
        row.revoked_at = utcnow()
        row.secret_ref = None
        row.health = "down"
        session.commit()
        session.refresh(row)
        out = row_to_view(row)

    audit(
        "connection.revoked",
        workspace_id=out.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=out.id,
        payload={"provider": out.provider},
    )
    events.record(
        connection_id=out.id,
        workspace_id=out.workspace_id,
        direction="write",
        operation="revoke",
        actor=actor_id,
        outcome="ok",
        simulated=out.simulated,
    )
    return out
