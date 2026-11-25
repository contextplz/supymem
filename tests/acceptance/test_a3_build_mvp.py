"""A3 — Build path offline with fake adapter."""

from __future__ import annotations

import pytest
from pathlib import Path
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.build.adr import clear_adrs, create_adr, lock_adr
from app.build.dispatcher import dispatch
from app.build.gates import run_all
from app.build.planner import plan_work_orders
from app.build.workorder import create_workorder
from coding_agents.consent import clear_consents, record_consent
from coding_agents.inhouse import InhouseAdapter


@pytest.mark.hosted_only
@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_a3_adr_then_wo_then_gates(tmp_path: Path):
    clear_adrs(); clear_consents(); record_consent("inhouse")
    adr = create_adr("ven_a3", title="MVP", stack=["fastapi"], protected_routes=["/api/me", "/api/items"])
    lock_adr(adr.id)
    wos = plan_work_orders("ven_a3")
    assert wos
    wo = wos[0]
    wo.files_expected = ["inhouse_change.txt"]
    res = await dispatch(wo, workdir=tmp_path, adapter=InhouseAdapter())
    assert res.ok
    green = run_all({
        "schema_ok": True, "static_findings": [], "unit_tests_ok": True, "scaffold_ok": True,
        "secret_leaks": [], "auth_live_ok": True, "components": wo.components, "vulns": [],
        "error_boundary": True, "migrations_ok": True, "author_agent_id": "b1",
        "reviewer_agent_id": "r1", "peer_verdict": "approve", "readiness_score": 0.9,
        "blocking_issues": [],
    })
    assert all(g.passed for g in green)
    # client-side auth fixture fails gate 6
    red6 = run_all({**{k: True for k in []}, "auth_live_ok": False, "schema_ok": True,
                    "static_findings": [], "unit_tests_ok": True, "scaffold_ok": True,
                    "secret_leaks": [], "components": ["a"], "vulns": [], "error_boundary": True,
                    "migrations_ok": True, "author_agent_id": "b1", "reviewer_agent_id": "r1",
                    "peer_verdict": "approve", "readiness_score": 1.0, "blocking_issues": []})
    assert any(g.name == "auth_enforcement" and not g.passed for g in red6)
