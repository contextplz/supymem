"""Charter synthesize + lock (deterministic when offline)."""

from __future__ import annotations

import json
from typing import Any

from core.canonical import digest
from core.clock import utcnow
from core.enums import ChannelKind, GoToMarketOrder
from core.ids import new_id


def synthesize_charter(
    *,
    idea: str,
    classification: dict[str, Any],
    answers: dict[str, Any],
    dissent: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gtm = answers.get("gtm_order", "market_first")
    if gtm not in {e.value for e in GoToMarketOrder}:
        gtm = "market_first"
    content = {
        "one_liner": idea.strip().split(".")[0][:120] or "Untitled venture",
        "problem": answers.get("problem") or idea[:280],
        "audience": answers.get("audience") or classification.get("audience") or "users",
        "jobs_to_be_done": answers.get("jobs_to_be_done") or ["get value quickly"],
        "mvp_scope": answers.get("mvp_scope") or ["core workflow"],
        "explicit_non_goals": answers.get("non_goals") or ["native mobile"],
        "business_model": classification.get("business_model", "saas"),
        "pricing_hypothesis": answers.get("monetization") or "TBD",
        "success_metric": "weekly_active_users",
        "success_threshold": 20.0,
        "build_archetype": classification.get("build_archetype", "crud_web"),
        "integrations": answers.get("integrations") or [],
        "regulatory_notes": [classification.get("regulatory_surface", "none")],
        "risks": [],
        "gtm_order": gtm,
        "channels": answers.get("channels") or [ChannelKind.LANDING.value],
        "brand": {"name": answers.get("brand_name") or "Venture", "tone": answers.get("brand_tone") or "clear"},
        "open_questions": [],
        "provenance": {
            k: {"source": "answered" if k in answers else "inferred", "value": v}
            for k, v in {
                "audience": answers.get("audience") or classification.get("audience"),
                "gtm_order": gtm,
                "business_model": classification.get("business_model"),
            }.items()
        },
    }
    return content


def lock_charter(
    *,
    venture_id: str,
    content: dict[str, Any],
    slots_snapshot: dict[str, Any],
    dissent: list[dict[str, Any]] | None = None,
    version: str = "1.0.0",
    parent_version: str | None = None,
) -> dict[str, Any]:
    checksum = digest(content)
    return {
        "id": new_id("chr"),
        "venture_id": venture_id,
        "version": version,
        "parent_version": parent_version,
        "locked": 1,
        "content_json": json.dumps(content),
        "checksum": checksum,
        "slots_snapshot_json": json.dumps(slots_snapshot),
        "dissent_json": json.dumps(dissent or []),
        "created_at": utcnow().isoformat(),
        "locked_at": utcnow().isoformat(),
    }
