"""Competitor research — never presents fixtures as web evidence."""

from __future__ import annotations

import json
from typing import Any

from app.ops.base_agent import BaseOpsAgent
from app.state import db
from core.clock import utcnow
from core.ids import new_id


class CompetitorResearchAgent(BaseOpsAgent):
    agent_type = "competitor_research"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        workspace_id = context.get("workspace_id") or ""
        # This synchronous agent has no research-provider integration yet.
        # Returning an empty list is more useful than inventing a competitor.
        profiles: list[dict[str, Any]] = []
        if workspace_id:
            self._persist(workspace_id, profiles)
        return {
            "summary": "Competitor scan is simulated; no live provider was invoked.",
            "competitors": profiles,
            "mode": "simulated",
            "reason": "no_live_research_provider",
        }

    def _persist(self, workspace_id: str, profiles: list[dict[str, Any]]) -> None:
        from app.db.models import OpsCompetitor

        factory = db()
        with factory() as session:
            now = utcnow()
            for profile in profiles:
                session.add(
                    OpsCompetitor(
                        id=new_id("oco"),
                        workspace_id=workspace_id,
                        name=profile["name"],
                        website=profile.get("website"),
                        pricing_info_json=json.dumps(profile.get("pricing") or {}),
                        positioning=profile.get("positioning"),
                        strengths_json=json.dumps(profile.get("strengths") or []),
                        weaknesses_json=json.dumps(profile.get("weaknesses") or []),
                        last_researched=now,
                        created_at=now,
                    )
                )
            session.commit()
