"""Trust panel: what has left this machine (43 §6, 46 T10.15).

Aggregates by default; individual rows only on `?detail=1`, and those rows carry
metadata only — no prompt or completion body is stored anywhere to return.

Paths omit the `/api` prefix: the console gateway strips it before forwarding,
the same as every other Company OS router.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.boundary import ledger, providers

router = APIRouter(tags=["boundary"])


@router.get("/workspaces/{workspace_id}/boundary")
def workspace_boundary(
    workspace_id: str,
    detail: bool = Query(default=False),
    limit: int = Query(default=ledger.DEFAULT_ROW_LIMIT, ge=1, le=1000),
):
    payload = ledger.summary(workspace_id)
    if detail:
        payload["rows"] = [row.to_dict() for row in ledger.rows(workspace_id, limit=limit)]
    return payload


@router.get("/workspaces/{workspace_id}/boundary/export")
def export_boundary(
    workspace_id: str,
    limit: int = Query(default=1000, ge=1, le=10_000),
):
    """Full export the founder can keep. Metadata only, by design."""
    return {
        "workspace_id": workspace_id,
        "summary": ledger.summary(workspace_id),
        "rows": [row.to_dict() for row in ledger.rows(workspace_id, limit=limit)],
        "providers": [p.to_dict() for p in providers.load().values()],
    }


@router.get("/boundary/providers")
def provider_positions():
    """Every provider's recorded training position, honestly labelled."""
    positions = [p.to_dict() for key, p in providers.load().items() if key != "__unknown__"]
    if not positions:
        raise HTTPException(500, detail={"error": {"code": "providers_unreadable"}})
    return {"items": positions, "unknown": providers.fallback().to_dict()}
