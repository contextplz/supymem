"""Team Composer DoD — fixture ideas, rationale, rejections, invariants."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.composer import compose, personalize
from app.org.catalog import load_catalog, load_fitness_registry

IDEAS = [
    ("simple", {"gtm_order": "product_first", "build_archetype": "crud_saas", "monetization": "free", "regulatory_surface": "none", "work_order_tracks": 1}, {"complexity_score": 0.25}, (4, 8)),
    ("medium", {"gtm_order": "product_first", "build_archetype": "crud_saas", "monetization": "subscription", "regulatory_surface": "none", "work_order_tracks": 2, "integrations": ["whatsapp"]}, {"complexity_score": 0.5}, (6, 11)),
    ("complex", {"gtm_order": "product_first", "build_archetype": "marketplace", "monetization": "take_rate", "regulatory_surface": "health", "work_order_tracks": 3, "integrations": ["a", "b", "c", "d"]}, {"complexity_score": 0.8}, (8, load_catalog()["max_active_agents"])),
]


def _charter(base):
    return {**base, "current_stage": "org_planning", "one_liner": "test", "armed_channels": 0}


def test_fixture_bands_and_rationale():
    reg = load_fitness_registry()
    for name, ch, brief, (lo, hi) in IDEAS:
        plan = compose(_charter(ch), brief, stage="org_planning")
        assert lo <= plan.agent_count <= hi, (name, plan.agent_count, lo, hi)
        assert plan.rationale and len(plan.rationale) == plan.agent_count
        assert plan.rejected, "must report rejected roles"
        orch = [r for r in plan.roles if r.role == "orchestrator"]
        assert len(orch) == 1
        has_b = any(r.role == "builder" for r in plan.roles)
        has_r = any(r.role == "reviewer" for r in plan.roles)
        assert has_b == has_r
        for r in plan.roles:
            assert r.fitness_metric in reg
            assert r.retire_when
            assert r.allowed_tools


def test_cap_reports_deferrals():
    ch = _charter({
        "gtm_order": "product_first",
        "build_archetype": "marketplace",
        "monetization": "take_rate",
        "regulatory_surface": "health",
        "work_order_tracks": 5,
        "integrations": ["a", "b", "c", "d", "e"],
        "armed_channels": 0,
    })
    plan = compose(ch, {"complexity_score": 0.9}, stage="org_planning", max_agents=6)
    assert plan.agent_count <= 6
    assert any("deferred" in r.get("reason", "") for r in plan.rejected)


@pytest.mark.parametrize("audience", ["Equipment maintenance teams", "Family hiking groups"])
def test_required_roles_fit_inside_every_team_cap(audience):
    charter = _charter({"gtm_order": "product_first", "build_archetype": "marketplace",
        "monetization": "subscription", "work_order_tracks": 5,
        "regulatory_surface": "health", "integrations": ["calendar", "billing", "support", "records"]})
    charter["one_liner"] = audience
    for cap in (1, 2, 3, 6, 12):
        plan = compose(charter, {"complexity_score": .9}, max_agents=cap)
        assert plan.agent_count <= cap
        roles = [selection.role for selection in plan.roles]
        assert roles.count("orchestrator") == 1
        assert ("builder" in roles) == ("reviewer" in roles)
        assert plan.deferred and all(item in plan.rejected for item in plan.deferred)
    for cap in (0, -1):
        with pytest.raises(ValueError, match="at least one seat"):
            compose(charter, max_agents=cap)


def test_personalize_cannot_change_headcount():
    plan = compose(_charter({
        "gtm_order": "product_first",
        "build_archetype": "crud_saas",
        "monetization": "subscription",
        "regulatory_surface": "none",
        "work_order_tracks": 1,
    }), {"complexity_score": 0.3})
    out = personalize(plan.roles, {"one_liner": "x"})
    assert len(out) == len(plan.roles)


def test_deterministic_20x():
    ch = _charter({
        "gtm_order": "product_first",
        "build_archetype": "crud_saas",
        "monetization": "subscription",
        "regulatory_surface": "none",
        "work_order_tracks": 2,
    })
    brief = {"complexity_score": 0.45}
    counts = {compose(ch, brief).agent_count for _ in range(20)}
    assert len(counts) == 1
