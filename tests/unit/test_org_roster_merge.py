"""Pack seats survive compose; catalog roles fill the rest."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.characters import canonical_role
from app.org.summary import merge_rosters


def test_merge_keeps_pack_people_and_adds_composed_roles():
    pack = [
        {"id": "researcher", "name": "Maya", "role": "research", "status": "ready"},
        {"id": "founder_concierge", "name": "Samira", "role": "concierge", "status": "ready"},
    ]
    composed = {
        "agents": [
            {"id": "agt_ren", "name": "Ren", "role": "orchestrator", "status": "active"},
            {"id": "agt_maya", "name": "Maya", "role": "researcher", "status": "quarantined"},
            {"id": "agt_cole", "name": "Cole", "role": "builder", "status": "quarantined"},
        ]
    }
    out = merge_rosters(composed, pack)
    names = {a["name"] for a in out["agents"]}
    assert {"Maya", "Samira", "Ren", "Cole"} <= names
    researchers = [
        a for a in out["agents"] if canonical_role(a.get("role")) == "researcher"
    ]
    assert len(researchers) == 1
    assert researchers[0]["id"] == "researcher"
    assert out["source"] == "composed+pack"


def test_merge_keeps_activated_status_from_composed():
    pack = [
        {"id": "researcher", "name": "Maya", "role": "research", "status": "ready"},
        {"id": "builder", "name": "Cole", "role": "build", "status": "ready"},
    ]
    composed = {
        "agents": [
            {"id": "researcher", "name": "Maya", "role": "research", "status": "active"},
            {"id": "agt_ren", "name": "Ren", "role": "orchestrator", "status": "active"},
        ]
    }
    out = merge_rosters(composed, pack)
    by_id = {a["id"]: a for a in out["agents"]}
    assert by_id["researcher"]["status"] == "active"
    assert by_id["builder"]["status"] == "ready"
    assert by_id["agt_ren"]["name"] == "Ren"
