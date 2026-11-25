from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.lifecycle import atomic_replace
from app.org.registry import registry


def test_atomic_replace_never_neither_nor_both():
    old = registry.create_agent_from_spec(
        venture_id="ven_swap",
        role="channel_operator",
        name="Old",
        charge="post",
        system_prompt="s",
        allowed_tools=["emit_signal"],
        model_tier="flash",
        fitness_metric="engagement_rate",
        retire_when="never",
        status="active",
    )
    result = atomic_replace(
        retire_id=old.id,
        new_spec_kwargs={
            "venture_id": "ven_swap",
            "role": "channel_operator",
            "name": "New",
            "charge": "post",
            "system_prompt": "s2",
            "allowed_tools": ["emit_signal"],
            "model_tier": "flash",
            "fitness_metric": "engagement_rate",
            "retire_when": "never",
        },
    )
    a = registry.get_instance(result["retired"])
    b = registry.get_instance(result["activated"])
    assert a.status == "retired"
    assert b.status == "active"
    actives = [x for x in registry.list_agents("ven_swap") if x.status == "active" and x.id in {a.id, b.id}]
    assert len(actives) == 1
