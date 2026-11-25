"""Keep social connections alive — the agent dies if auth dies."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.clock import utcnow
from sqlalchemy import select

from app.content import composio_social
from app.content.oauth import flow as oauth_flow
from app.db.models import ContentAccount
from app.state import db

log = logging.getLogger(__name__)


def _extra(row: ContentAccount) -> dict[str, Any]:
    try:
        data = json.loads(row.extra_json or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def check_and_heal(workspace_id: str) -> dict[str, Any]:
    """Reconcile local accounts with Composio; mark dead links; sync live ones.

    Returns a health report the agent tick and UI both read.
    """
    report: dict[str, Any] = {
        "workspace_id": workspace_id,
        "composio": composio_social.configured(),
        "accounts": [],
        "needs_reconnect": [],
        "healed": [],
    }
    if not composio_social.configured():
        report["reason"] = "composio_missing"
        return report

    # Pull every connection (including EXPIRED) for this content user.
    remote = composio_social.list_connections(
        workspace_id=workspace_id, active_only=False
    )
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for conn in remote:
        by_platform.setdefault(conn["platform"], []).append(conn)

    # Prefer ACTIVE; otherwise note reconnect.
    for platform, conns in by_platform.items():
        active = [c for c in conns if c["status"] in {"ACTIVE", "SUCCESS", "CONNECTED"}]
        if active:
            try:
                synced = composio_social.sync_workspace(
                    workspace_id, platform=platform
                )
                report["healed"].extend(synced)
            except Exception as exc:
                log.warning("keepalive sync %s failed: %s", platform, exc)
        else:
            report["needs_reconnect"].append(
                {
                    "platform": platform,
                    "remote_status": conns[0]["status"] if conns else "missing",
                    "connected_account_id": conns[0]["id"] if conns else None,
                }
            )

    factory = db()
    now = utcnow()
    with factory() as session:
        rows = session.scalars(
            select(ContentAccount).where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.status != "revoked",
            )
        ).all()
        for row in rows:
            extra = _extra(row)
            ca = str(extra.get("connected_account_id") or "")
            remote_match = next(
                (c for c in remote if c.get("id") == ca),
                None,
            )
            status = "unknown"
            if remote_match:
                status = remote_match["status"]
            elif extra.get("auth_via") == "composio":
                status = "MISSING"
            else:
                status = "native"

            if extra.get("auth_via") == "composio" and os.environ.get("COMPOSIO_PROJECT") and extra.get("composio_project", "v1") != os.environ["COMPOSIO_PROJECT"]:
                status = "MISSING"
            healthy = status in {"ACTIVE", "SUCCESS", "CONNECTED", "native"}
            if not healthy and row.status == "active":
                row.status = "needs_reconnect" if status == "MISSING" else "expired"
                row.health = "down"
                row.paused_reason = f"composio:{status}"
                row.last_error = None if status == "MISSING" else (
                    f"Connection {status}. Agent paused this channel until you reconnect."
                )[:500]
                row.updated_at = now
                if row.platform not in {n["platform"] for n in report["needs_reconnect"]}:
                    report["needs_reconnect"].append(
                        {
                            "platform": row.platform,
                            "remote_status": status,
                            "connected_account_id": ca or None,
                        }
                    )
            elif healthy and row.status in {"expired", "needs_reconnect"}:
                row.status = "active"
                row.health = "ok"
                row.paused_reason = None
                row.last_error = None
                row.updated_at = now

            report["accounts"].append(
                {
                    "id": row.id,
                    "platform": row.platform,
                    "handle": row.handle,
                    "local_status": row.status,
                    "remote_status": status,
                    "healthy": healthy,
                    "auth_via": extra.get("auth_via") or "native",
                }
            )
        session.commit()

    from app.content.engine import record_event

    if report["needs_reconnect"]:
        record_event(
            workspace_id=workspace_id,
            kind="agent.reconnect_needed",
            payload={"channels": report["needs_reconnect"]},
        )
    else:
        record_event(
            workspace_id=workspace_id,
            kind="agent.keepalive_ok",
            payload={"accounts": len(report["accounts"])},
        )
    return report


def mint_reconnect_link(workspace_id: str, platform: str) -> dict[str, str]:
    return composio_social.start_connect(workspace_id=workspace_id, platform=platform)
