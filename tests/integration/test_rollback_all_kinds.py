from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.rollback import rollback


def test_rollback_each_kind():
    for kind in ["agent_spec", "workflow", "tool", "org", "product_code", "strategy", "config"]:
        r = rollback({"target_kind": kind, "baseline": {"metric": "x"}}, reason="test")
        assert r.ok, kind
