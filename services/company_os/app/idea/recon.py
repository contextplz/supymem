"""idea.recon job — uses existing research_recon, never fabricates hits."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.db.supymem_models import IdeaSession, ResearchDossier, ResearchFinding
from app.state import db
from core.clock import utcnow
from core.ids import new_id


async def run_recon(payload: dict[str, Any]) -> None:
    from app.signals.research import research_recon

    idea = str(payload.get("text") or "")
    recon = await research_recon(idea, budget_s=20.0)
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(IdeaSession, payload["session_id"])
        if row is None:
            return
        row.recon_summary = json.dumps(recon, sort_keys=True, default=str)
        dossier = ResearchDossier(
            id=new_id("rsd"),
            workspace_id=row.workspace_id,
            venture_id=row.venture_id,
            revision_id=row.current_revision_id,
            kind="recon",
            state="complete" if recon else "partial",
            budget_calls=6,
            calls_used=int(recon.get("calls_used") or 0),
            summary=str(recon.get("summary") or recon.get("reason") or ""),
            created_at=now,
            completed_at=now,
        )
        session.add(dossier)
        for i, comp in enumerate((recon.get("competitors") or [])[:8]):
            if isinstance(comp, str):
                title, url = comp, None
            else:
                title = str(comp.get("name") or comp.get("title") or f"competitor-{i}")
                url = comp.get("url")
            session.add(
                ResearchFinding(
                    id=new_id("rsf"),
                    dossier_id=dossier.id,
                    workspace_id=row.workspace_id,
                    venture_id=row.venture_id,
                    category="competitor_direct",
                    title=title[:240],
                    url=url,
                    source=str(recon.get("provider") or "recon"),
                    summary="",
                    evidence=json.dumps(comp, default=str) if not isinstance(comp, str) else "{}",
                    created_at=now,
                )
            )
        session.commit()
    from app.idea.service import run_clarify_turn

    run_clarify_turn(payload)


def run_recon_sync(payload: dict[str, Any]) -> None:
    from app.worker.run_sync import run_kind

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_recon(payload))
        return
    run_kind("idea.recon", payload)
