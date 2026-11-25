#!/usr/bin/env python3
"""Eval: 12 CHAT_STEERING prompts against a fixture venture + offline LLM.

Run via pytest (recommended):
  python -m pytest tests/unit/chat/test_chat_steering_eval.py -q

Or:
  python -m pytest scripts/eval_chat_steering.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.company_os_harness import company_os_test_client, load_company_os_app

PROMPTS = [
    ("1", "Hey, what's going on with LabHire right now? What do you need from me?", ["labhire", "need"]),
    ("2", "What did we decide about who LabHire is for?", ["labhire", "engineer"]),
    ("3", "What happened with the last Mac task you ran?", ["mac"]),
    ("4", "Is my Gmail connected? What can you do with it?", ["gmail"]),
    ("5", "Why did the last build fail and what should we do about it?", ["fail", "provider"]),
    ("6", "Show me how the waitlist is doing.", ["waitlist", "0"]),
    ("7", "Draft a short outreach email to a first prospect for LabHire. Do not send it.", ["draft"]),
    ("8", "Update the plan: our first milestone is 10 waitlist signups this week.", ["queue", "waitlist", "10"]),
    ("9", "Research three competitors of LabHire.", ["research", "confirm"]),
    ("10", "What should I do next, in order?", ["need", "next", "open", "You're at"]),
    ("11", "Remember that I prefer short answers.", ["remember", "short"]),
    ("12", "What were we just talking about?", ["talk", "waitlist", "gmail", "build", "research", "earlier", "just"]),
]

STATUS_MARKERS = ("**Now**", "**Needs you**", "**Next**", "**Being done now**", "**Blockers**")
BULLET_RE = re.compile(r"(?m)^\s*[-*•]\s+")

BEFORE_CARD = (
    "LabHire – Computer use · autopilot on · L1\n\n"
    "**Now**\n- Computer use: …\n\n**Needs you**\n- Start reviewed Mac task\n\n"
    "**Next**\n- Start reviewed Mac task"
)


def _seed_fixture(vid: str, wid: str, data_dir: Path) -> None:
    import json as _json

    from app.db.models import GtmEmailAccount, IdeaBrief, VentureDocument
    from app.db.supymem_models import BuildRun, FounderApp
    from app.state import MEM, db
    from core.clock import utcnow
    from core.ids import new_id

    MEM[f"website:{vid}"] = {"url": "https://labhire.example", "published": True, "live": True}

    factory = db()
    with factory() as session:
        session.add(
            IdeaBrief(
                id=new_id("ibr"),
                venture_id=vid,
                domain="jobs",
                business_model="marketplace",
                build_archetype="web",
                audience="QA engineers looking for roles that match their stack",
                regulatory_surface="none",
                novelty="medium",
                complexity_score=0.4,
                uncertainty_score=0.5,
                build_feasibility=0.7,
                complexity_factors_json="{}",
                research_notes_json="{}",
                classifier_version="eval",
                created_at=utcnow(),
            )
        )
        app_id = new_id("app")
        session.add(
            FounderApp(
                id=app_id,
                workspace_id=wid,
                venture_id=vid,
                name="LabHire",
                slug=f"labhire-{app_id[-8:]}",
                kind="web",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.flush()
        session.add(
            BuildRun(
                id=new_id("bld"),
                app_id=app_id,
                workspace_id=wid,
                venture_id=vid,
                trigger="eval",
                agent="managed",
                state="failed",
                spec=_json.dumps({"failure_reason": "provider down"}),
                credits_used=153_594,
                error_code="provider_down",
                exit_summary="provider down",
                runtime_version=1,
                created_at=utcnow(),
            )
        )
        session.add(
            GtmEmailAccount(
                id=new_id("gea"),
                workspace_id=wid,
                provider="gmail",
                email="founder-secret@example.com",
                composio_user_id="cu_eval",
                connected_account_id="ca_eval",
                status="active",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.add(
            VentureDocument(
                id=new_id("dwo"),
                venture_id=vid,
                kind="device_work_order_v1",
                title="Verify venture workspace in terminal",
                path=None,
                body_md=None,
                provenance_json=_json.dumps(
                    {"status": "completed", "manifest": {"task_id": "t1"}}
                ),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.commit()


def _entity_hit(prompt_id: str, body: str, keys: list[str], kind: str | None) -> bool:
    lower = body.lower()
    if prompt_id == "5":
        return "provider" in lower and "credit" not in lower and "fail" in lower
    if prompt_id == "9":
        return kind == "confirmation" or (
            "research" in lower and any(k in lower for k in ("confirm", "credit", "queue", "idea"))
        )
    if prompt_id == "7":
        return "draft" in lower or "email" in lower
    if prompt_id == "8":
        return any(k in lower for k in ("is starting", "samira", "autopilot", "milestone", "waitlist", "saved plan to update"))
    if prompt_id == "11":
        return "remember" in lower or "short" in lower
    if prompt_id == "12":
        return any(k in lower for k in keys)
    return any(k in lower for k in keys)


@pytest.fixture()
def eval_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/eval.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with company_os_test_client(main.create_app()) as client:
        from app.core.local_mode import is_local
        if is_local():
            selected = client.post("/local/providers/sample", json={})
            assert selected.status_code == 200, selected.text
        created = client.post(
            "/ventures",
            json={"raw_idea": "LabHire helps QA engineers find matching roles", "name": "LabHire"},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        vid = payload["id"]
        wid = payload["workspace_id"]
        _seed_fixture(vid, wid, tmp_path)
        from app.ventures.state_brief import invalidate_venture_state_brief

        invalidate_venture_state_brief(vid)

        async def _empty(*_a, **_k):
            return ""

        monkeypatch.setattr("app.chat.assistant._complete", _empty)
        yield client, vid, tmp_path


@pytest.mark.asyncio
async def test_chat_steering_eval_twelve_prompts(eval_env):
    _client, vid, tmp_path = eval_env
    from app.chat.router import handle_message

    results = []
    before_after = []
    for pid, prompt, keys in PROMPTS:
        reply = await handle_message(vid, prompt)
        body = str(reply.get("body_md") or "")
        kind = reply.get("kind")
        ok_entity = _entity_hit(pid, body, keys, kind)
        ok_no_card = not any(m in body for m in STATUS_MARKERS)
        ok_no_bullets = BULLET_RE.search(body) is None or kind == "confirmation" or pid in {"9", "10"}
        ok_build = True
        if pid == "5":
            ok_build = "credit" not in body.lower() and "provider" in body.lower()
        passed = ok_entity and ok_no_card and ok_no_bullets and ok_build
        results.append((pid, passed, body, kind))
        before_after.append(
            {
                "id": pid,
                "prompt": prompt,
                "before": BEFORE_CARD if pid in {"1", "2", "3", "4", "5", "10", "12"} else "(action / other)",
                "after": body,
                "kind": kind,
                "pass": passed,
            }
        )

    (tmp_path / "chat_steering_eval.json").write_text(
        json.dumps(before_after, indent=2), encoding="utf-8"
    )

    failed = [r for r in results if not r[1]]
    summary = "\n".join(
        f"[{'PASS' if p else 'FAIL'}] #{pid} kind={kind} :: {body[:100].replace(chr(10), ' ')}"
        for pid, p, body, kind in results
    )
    assert not failed, f"CHAT_STEERING eval failed:\n{summary}"
