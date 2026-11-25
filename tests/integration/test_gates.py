from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.build.gates import GATE_NAMES, run_all


def _green_ctx():
    return {
        "schema_ok": True,
        "static_findings": [],
        "unit_tests_ok": True,
        "scaffold_ok": True,
        "secret_leaks": [],
        "auth_live_ok": True,
        "components": ["a", "b"],
        "vulns": [],
        "error_boundary": True,
        "migrations_ok": True,
        "author_agent_id": "a1",
        "reviewer_agent_id": "a2",
        "peer_verdict": "approve",
        "readiness_score": 1.0,
        "blocking_issues": [],
        "protected_routes": ["/api/me"],
    }


def test_twelve_green():
    results = run_all(_green_ctx())
    assert len(results) == 12
    assert {r.name for r in results} == set(GATE_NAMES)
    assert all(r.passed for r in results)


def test_twelve_red():
    red = {
        "schema_ok": False,
        "static_findings": ["x"],
        "unit_tests_ok": False,
        "scaffold_ok": False,
        "secret_leaks": ["key"],
        "auth_live_ok": False,
        "components": [f"c{i}" for i in range(25)],
        "vulns": ["CVE"],
        "error_boundary": False,
        "migrations_ok": False,
        "author_agent_id": "a1",
        "reviewer_agent_id": "a1",
        "peer_verdict": "reject",
        "readiness_score": 0.1,
        "blocking_issues": ["auth"],
    }
    results = run_all(red)
    assert all(not r.passed for r in results)
