"""Engagement create/read for the public /api/v1 surface (doc 41).

All logic lives in ``app.engagements.service``; this module only adapts
signatures and error shapes for surface callers.
"""

from __future__ import annotations

from typing import Any

from app.surfaces.errors import SurfaceError


def read_engagement(engagement_id: str) -> dict[str, Any] | None:
    from app.engagements import service as eng

    view = eng.get_engagement(engagement_id)
    return view.to_dict() if view is not None else None


async def start_engagement(
    *,
    workspace_id: str,
    request_text: str,
    requested_by: str,
    origin: str = "api",
    origin_ref: str | None = None,
) -> dict[str, Any]:
    from app.engagements import service as eng

    try:
        view = await eng.create_engagement(
            workspace_id=workspace_id,
            text=request_text,
            origin=origin if origin in eng.VALID_ORIGINS else "api",
            requested_by=requested_by,
            origin_ref=origin_ref,
            surface="api",
        )
    except eng.EngagementError as exc:
        raise SurfaceError(
            getattr(exc, "code", "engagement_error"),
            str(exc),
            status=422,
        ) from exc
    return view.to_dict()
