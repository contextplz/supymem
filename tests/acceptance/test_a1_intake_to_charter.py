"""A1 — Idea to Charter (offline/API level)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

main = load_company_os_app()

IDEAS = [
    ("a todo app for college students", (3, 4)),
    (
        "an app that helps home bakers take custom cake orders on WhatsApp instead of losing them in DMs",
        (5, 6),
    ),
    (
        "a marketplace connecting physiotherapists with clinics that need temporary cover, with insurance billing and background checks",
        (8, 9),
    ),
]


@pytest.mark.acceptance
def test_a1_question_bands_and_charter_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/a1.db")
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())
    for idea, (lo, hi) in IDEAS:
        ven = client.post("/ventures", json={"name": "Home baker orders", "raw_idea": idea}).json()
        intake = client.post(f"/ventures/{ven['id']}/intake").json()
        qb = intake["question_budget"]
        n = qb.get("final", qb.get("n"))
        assert lo <= n <= hi, (idea, n, qb)
        assert qb.get("reasons") or qb.get("contributions")
        qs = intake["questions"]
        for q in qs:
            assert 3 <= len(q["options"]) <= 5
            assert sum(1 for o in q["options"] if o.get("recommended")) == 1
            assert q.get("allow_other") is True
        answers = {q["id"]: q["options"][0]["id"] for q in qs}
        locked = client.post(
            f"/ventures/{ven['id']}/charter/lock",
            json={"answers": answers, "other_verbatim": {}},
        ).json()
        assert locked["checksum"]
        content = locked["content"]
        assert content.get("explicit_non_goals")
        assert content.get("success_metric")
        assert content.get("success_threshold") is not None
        assert content.get("provenance")
