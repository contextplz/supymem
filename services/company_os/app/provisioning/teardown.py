"""Idempotent teardown of venture resources."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sqlalchemy import select

from app.db.models import Connection, Venture, VentureResource
from app.provisioning.adapters import host_local
from app.state import db


class ConnectionBackedTeardown(PermissionError):
    """Provisioning must never tear down a system they own (39 §2, R14)."""


def _connection_targets(session, venture_id: str) -> list[str]:
    ven = session.get(Venture, venture_id)
    if ven is None or not ven.workspace_id:
        return []
    rows = session.scalars(
        select(Connection).where(
            Connection.workspace_id == ven.workspace_id,
            Connection.status != "revoked",
        )
    ).all()
    return [f"{r.provider}:{r.external_account}" for r in rows]


async def teardown_venture(venture_id: str) -> None:
    factory = db()
    with factory() as session:
        owned = _connection_targets(session, venture_id)
        if owned:
            raise ConnectionBackedTeardown(
                "Refusing to tear down Connection-backed targets: " + ", ".join(owned)
            )
        rows = session.scalars(
            select(VentureResource).where(VentureResource.venture_id == venture_id)
        ).all()
        for row in list(rows):
            data = {}
            if row.teardown_json:
                try:
                    data = json.loads(row.teardown_json)
                except json.JSONDecodeError:
                    data = {}
            if row.resource == "hosting" and data.get("port"):
                host_local.release_port(int(data["port"]))
            if data.get("path"):
                p = Path(data["path"])
                if p.exists() and "ventures" in str(p):
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
            session.delete(row)
        session.commit()
