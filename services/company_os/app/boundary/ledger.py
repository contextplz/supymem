"""The No-Train Ledger (43 §6). Append-only: insert, never update.

One row per outbound model call, including refused ones, written *before* the
call leaves so a crash mid-flight still leaves evidence of what was about to go
out. `bytes_in` and `cost_usd` therefore stay zero on the pre-send row: the
table's triggers reject UPDATE, and a second row would break "one row per call".
Response-side accounting reconciles through `trace_id`.

Bodies are never stored. There is no column for a prompt or a completion, and
this module does not write content or a content digest into the columns there
are.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select

from app.boundary import providers
from app.db.models import BoundaryLedgerEntry, Workspace
from app.state import db
from core.clock import utcnow
from core.ids import new_id

# A trust panel that has to page a million rows is a trust panel nobody opens.
DEFAULT_ROW_LIMIT = 200


class DebugStorageRefused(RuntimeError):
    """DEBUG_STORE_PROMPTS is not available outside a fully local install."""


@dataclass(frozen=True)
class LedgerRow:
    id: str
    workspace_id: str | None
    at: str
    provider: str
    purpose: str
    classes: tuple[str, ...]
    bytes_out: int
    bytes_in: int
    redactions: int
    blocked: bool
    block_reason: str | None
    no_train_asserted: bool
    cost_usd: float
    trace_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "provider": self.provider,
            "purpose": self.purpose,
            "classes": list(self.classes),
            "bytes_out": self.bytes_out,
            "bytes_in": self.bytes_in,
            "redactions": self.redactions,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "no_train_asserted": self.no_train_asserted,
            "cost_usd": self.cost_usd,
            "trace_id": self.trace_id,
            # Bodies are not stored at all by default (43 §7), so there is
            # nothing to hash and nothing to expand into.
            "content": "not_stored",
        }


def _view(row: BoundaryLedgerEntry) -> LedgerRow:
    try:
        classes = tuple(json.loads(row.classes_json or "[]"))
    except json.JSONDecodeError:
        classes = ()
    return LedgerRow(
        id=row.id,
        workspace_id=row.workspace_id,
        at=row.at.isoformat() if row.at else "",
        provider=row.provider,
        purpose=row.purpose,
        classes=classes,
        bytes_out=int(row.bytes_out or 0),
        bytes_in=int(row.bytes_in or 0),
        redactions=int(row.redactions or 0),
        blocked=bool(row.blocked),
        block_reason=row.block_reason,
        no_train_asserted=bool(row.no_train_asserted),
        cost_usd=float(row.cost_usd or 0.0),
        trace_id=row.trace_id,
    )


def record(
    *,
    workspace_id: str | None,
    provider: str,
    purpose: str,
    classes: Sequence[str],
    bytes_out: int,
    bytes_in: int = 0,
    redactions: int = 0,
    blocked: bool = False,
    block_reason: str | None = None,
    cost_usd: float = 0.0,
    trace_id: str | None = None,
) -> str:
    """Insert one row and return its id. Raises if it cannot be written."""
    row_id = new_id("bnd")
    factory = db()
    with factory() as session:
        session.add(
            BoundaryLedgerEntry(
                id=row_id,
                workspace_id=workspace_id,
                at=utcnow(),
                provider=provider,
                purpose=purpose,
                classes_json=json.dumps(list(dict.fromkeys(str(c) for c in classes))),
                bytes_out=int(bytes_out),
                bytes_in=int(bytes_in),
                redactions=int(redactions),
                blocked=1 if blocked else 0,
                block_reason=block_reason,
                no_train_asserted=1 if providers.no_train_asserted(provider) else 0,
                cost_usd=float(cost_usd),
                trace_id=trace_id,
            )
        )
        session.commit()
    return row_id


def rows(
    workspace_id: str | None = None, *, limit: int = DEFAULT_ROW_LIMIT
) -> list[LedgerRow]:
    factory = db()
    with factory() as session:
        stmt = select(BoundaryLedgerEntry)
        if workspace_id is not None:
            stmt = stmt.where(BoundaryLedgerEntry.workspace_id == workspace_id)
        stmt = stmt.order_by(BoundaryLedgerEntry.at.desc(), BoundaryLedgerEntry.id.desc())
        return [_view(r) for r in session.scalars(stmt.limit(limit)).all()]


def summary(workspace_id: str | None = None, *, limit: int = 10_000) -> dict[str, Any]:
    """"What has left this machine": aggregates by provider and by class."""
    entries = rows(workspace_id, limit=limit)
    by_provider: dict[str, dict[str, Any]] = {}
    by_class: dict[str, dict[str, Any]] = {}
    totals = {
        "calls": 0,
        "blocked": 0,
        "bytes_out": 0,
        "bytes_in": 0,
        "redactions": 0,
        "cost_usd": 0.0,
    }

    for row in entries:
        totals["calls"] += 1
        totals["blocked"] += 1 if row.blocked else 0
        totals["redactions"] += row.redactions
        totals["cost_usd"] = round(totals["cost_usd"] + row.cost_usd, 6)
        if not row.blocked:
            totals["bytes_out"] += row.bytes_out
            totals["bytes_in"] += row.bytes_in

        position = providers.position_for(row.provider)
        bucket = by_provider.setdefault(
            row.provider,
            {
                "provider": row.provider,
                "calls": 0,
                "blocked": 0,
                "bytes_out": 0,
                "redactions": 0,
                "cost_usd": 0.0,
                "no_train_asserted": row.no_train_asserted,
                "no_train_note": position.ui_string,
                "source_url": position.source_url,
                "date_checked": position.date_checked,
            },
        )
        bucket["calls"] += 1
        bucket["blocked"] += 1 if row.blocked else 0
        bucket["redactions"] += row.redactions
        bucket["cost_usd"] = round(bucket["cost_usd"] + row.cost_usd, 6)
        if not row.blocked:
            bucket["bytes_out"] += row.bytes_out
        # One unasserted call is enough to stop claiming the provider asserts it.
        bucket["no_train_asserted"] = bucket["no_train_asserted"] and row.no_train_asserted

        for name in row.classes:
            cls = by_class.setdefault(
                name, {"class": name, "calls": 0, "blocked": 0, "bytes_out": 0}
            )
            cls["calls"] += 1
            cls["blocked"] += 1 if row.blocked else 0
            if not row.blocked:
                cls["bytes_out"] += row.bytes_out

    blocked_rows = [
        {
            "at": row.at,
            "provider": row.provider,
            "purpose": row.purpose,
            "classes": list(row.classes),
            "reason": row.block_reason,
        }
        for row in entries
        if row.blocked
    ]
    return {
        "workspace_id": workspace_id,
        "totals": totals,
        "by_provider": sorted(by_provider.values(), key=lambda b: b["provider"]),
        "by_class": sorted(by_class.values(), key=lambda b: b["class"]),
        "blocked": blocked_rows,
        "prompt_bodies_stored": False,
    }


def debug_prompt_storage_enabled() -> bool:
    return (os.getenv("DEBUG_STORE_PROMPTS") or "").strip().lower() in {"1", "true", "yes"}


def assert_debug_storage_allowed() -> None:
    """Refuse prompt storage entirely once any workspace is not local (43 §7)."""
    if not debug_prompt_storage_enabled():
        return
    factory = db()
    with factory() as session:
        non_local = session.scalars(
            select(Workspace.id).where(Workspace.tenancy != "local")
        ).first()
    if non_local is not None:
        raise DebugStorageRefused(
            "Prompt storage is off: a workspace here is not local, and debug "
            "convenience must not become a data lake."
        )


__all__ = [
    "DEFAULT_ROW_LIMIT",
    "DebugStorageRefused",
    "LedgerRow",
    "assert_debug_storage_allowed",
    "debug_prompt_storage_enabled",
    "record",
    "rows",
    "summary",
]
