"""A8 — Guided onboarding, cold start, local only (34 §2)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

FORBIDDEN = re.compile(
    r"\b(execute|invoke|API|endpoint|payload|schema|JSON|LLM|prompt|token)\b",
    re.I,
)
ID_PAT = re.compile(r"\b(ven|chr|obr|qst|tsk)_[A-Za-z0-9]+\b")
SNAKE_OPT = re.compile(r"^[a-z]+_[a-z]+$")


@pytest.mark.acceptance
def test_a8_guided_onboarding(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/a8.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "1")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")

    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())

    idea = "a marketplace for restoring vintage synthesizers"
    created = client.post(
        "/ventures",
        json={"raw_idea": idea, "mode": "guided"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body.get("name") and body["name"].lower() not in {"untitled", "t", "asdf"}
    vid = body["id"]
    # New-style create auto-starts; should already be paused at clarification
    onboard = body.get("onboarding") or {}
    if onboard.get("status") != "paused":
        r = client.post(f"/ventures/{vid}/onboard", json={"mode": "guided"})
        assert r.status_code in (200, 202), r.text

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "paused"
    assert status["paused_reason"] == "awaiting_product_understanding"
    assert status["phase"] == "understand"

    product_round = client.get(f"/ventures/{vid}/clarification")
    assert product_round.status_code == 200, product_round.text
    product_qs = product_round.json()["questions"]
    assert product_round.json()["process"] == "product_understanding"
    assert 3 <= len(product_qs) <= 6
    product_answers = []
    for q in product_qs:
        rec = next(o for o in q["options"] if o.get("recommended"))
        if q.get("slot_key") == "product_today":
            assert q["selection_mode"] == "multiple"
            product_answers.append({"question_id": q["id"], "option_ids": ["opt_a", "opt_b"]})
        else:
            product_answers.append({"question_id": q["id"], "option_id": rec["id"]})
    posted = client.post(
        f"/ventures/{vid}/clarification/answers",
        json={"answers": product_answers},
    )
    assert posted.status_code == 202, posted.text

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "paused", status
    assert status["paused_reason"] == "awaiting_product_brief", status
    brief = client.get(f"/ventures/{vid}/product/brief")
    assert brief.status_code == 200, brief.text
    assert brief.json()["brief"]["what_it_is"]
    assert "named workaround" in brief.json()["brief"]["today"].lower()
    assert "already pay" in brief.json()["brief"]["today"].lower()
    assert client.post(f"/ventures/{vid}/product/brief", json={"action": "accept"}).status_code == 200

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "paused", status
    assert status["paused_reason"] == "awaiting_clarification", status
    assert status["phase"] == "validate"

    clar = client.get(f"/ventures/{vid}/clarification")
    assert clar.status_code == 200, clar.text
    questions = clar.json()["questions"]
    assert 3 <= len(questions) <= 9
    for q in questions:
        opts = q["options"]
        assert sum(1 for o in opts if o.get("recommended")) == 1
        assert q.get("allow_other") is True
        assert q.get("why_asked")
        for o in opts:
            assert o.get("implication")
            assert not SNAKE_OPT.match(o.get("label") or "")

    answers = []
    for q in questions:
        rec = next(o for o in q["options"] if o.get("recommended"))
        answers.append({"question_id": q["id"], "option_id": rec["id"]})

    posted = client.post(
        f"/ventures/{vid}/clarification/answers",
        json={"answers": answers},
    )
    assert posted.status_code == 202, posted.text

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "paused", status
    assert status["paused_reason"] == "awaiting_gstack_plan", status
    plan = client.get(f"/ventures/{vid}/gstack/plan")
    assert plan.status_code == 200, plan.text
    body = plan.json()["plan"]
    assert body["process"] == "gstack_office_hours"
    assert body["verdict"]
    assert len(body["next_five"]) == 5
    assert client.post(f"/ventures/{vid}/gstack/plan", json={"action": "accept"}).status_code == 200

    # Validation gate (doc 78) — must not silently skip in guided mode.
    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "paused", status
    assert status["paused_reason"] == "awaiting_validation_review", status
    review = client.post(
        f"/ventures/{vid}/validation/decision",
        json={"action": "review_summary"},
    )
    assert review.status_code == 200, review.text
    status = client.get(f"/ventures/{vid}/onboarding").json()
    if status["status"] == "paused" and status.get("paused_reason") == "awaiting_validation_decision":
        decide = client.post(
            f"/ventures/{vid}/validation/decision",
            json={"action": "proceed"},
        )
        assert decide.status_code == 200, decide.text

    from app.onboarding.registry import phases_for_mode

    status = client.get(f"/ventures/{vid}/onboarding").json()
    assert status["status"] == "done", status
    assert status["phases_done"] == len(phases_for_mode("idea"))
    assert status["cost_usd"] <= 0.30

    dash = client.get(f"/ventures/{vid}/dashboard").json()
    tiles = dash["tiles"]
    for key in (
        "next_up",
        "tasks",
        "website",
        "documents",
        "email",
        "social",
        "business",
        "team",
        "validation",
        "cost",
        "evolution",
    ):
        assert key in tiles, key
        assert tiles[key].get("state") in {"ready", "empty", "degraded", "loading", "error"}

    assert tiles["website"]["state"] == "ready"
    assert tiles["website"].get("simulated") is True
    assert tiles["validation"]["questions_answered"] >= 3
    assert tiles["validation"].get("unvalidated") is False
    # Slate is exactly five (doc 79); tolerate older dashboards that still expose three.
    assert len(tiles["next_up"]["tasks"]) in {3, 5}
    for t in tiles["next_up"]["tasks"]:
        assert t.get("why_now")
    assert tiles["team"]["active"] >= 3
    assert len(tiles["documents"]["items"]) >= 3

    activity = client.get(f"/ventures/{vid}/activity").json()
    items = activity["items"]
    assert items
    seqs = [i["seq"] for i in items]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    searches = [i for i in items if i.get("kind") == "search"]
    assert len(searches) >= 2
    for s in searches:
        assert s.get("query")
    for i in items:
        assert len(i["text"]) <= 72
        assert not FORBIDDEN.search(i["text"])
        assert not ID_PAT.search(i["text"])

    chat = client.get(f"/ventures/{vid}/chat").json()
    msgs = chat["items"]
    recaps = [m for m in msgs if m.get("kind") == "recap" and m.get("role") == "assistant"]
    assert recaps
    assert recaps[-1].get("citations")
